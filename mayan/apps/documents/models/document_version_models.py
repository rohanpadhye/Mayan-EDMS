import hashlib
import logging
import os
import shutil

from django.apps import apps
from django.db import models, transaction
from django.urls import reverse
from django.utils.encoding import force_text
from django.utils.functional import cached_property
from django.utils.translation import ugettext_lazy as _

from mayan.apps.common.classes import ModelQueryFields
from mayan.apps.common.signals import signal_mayan_pre_save
from mayan.apps.converter.classes import ConverterBase
from mayan.apps.converter.exceptions import InvalidOfficeFormat, PageCountError
from mayan.apps.converter.layers import layer_saved_transformations
from mayan.apps.converter.transformations import TransformationRotate
from mayan.apps.mimetype.api import get_mimetype
from mayan.apps.storage.classes import DefinedStorageLazy
from mayan.apps.templating.classes import Template

from ..events import event_document_file_new, event_document_file_revert
from ..literals import STORAGE_NAME_DOCUMENT_VERSION_PAGE_IMAGE_CACHE
#from ..managers import DocumentFileManager
from ..signals import signal_post_document_created, signal_post_file_upload

from .document_models import Document

__all__ = ('DocumentVersion',)
logger = logging.getLogger(name=__name__)


class DocumentVersion(models.Model):
    document = models.ForeignKey(
        on_delete=models.CASCADE, related_name='versions', to=Document,
        verbose_name=_('Document')
    )
    timestamp = models.DateTimeField(
        auto_now_add=True, db_index=True, help_text=_(
            'The server date and time when the document version was created.'
        ), verbose_name=_('Timestamp')
    )
    comment = models.TextField(
        blank=True, default='', help_text=_(
            'An optional short text describing the document version.'
        ), verbose_name=_('Comment')
    )

    class Meta:
        ordering = ('timestamp',)
        verbose_name = _('Document version')
        verbose_name_plural = _('Document versions')

    def __str__(self):
        return self.get_rendered_string()

    @cached_property
    def cache(self):
        Cache = apps.get_model(app_label='file_caching', model_name='Cache')
        return Cache.objects.get(
            defined_storage_name=STORAGE_NAME_DOCUMENT_VERSION_PAGE_IMAGE_CACHE
        )

    @cached_property
    def cache_partition(self):
        partition, created = self.cache.partitions.get_or_create(
            name='version-{}'.format(self.uuid)
        )
        return partition

    def delete(self, *args, **kwargs):
        for page in self.pages.all():
            page.delete()

        return super().delete(*args, **kwargs)

    #def fix_orientation(self):
    #    for page in self.pages.all():
    #        degrees = page.detect_orientation()
    #        if degrees:
    #            layer_saved_transformations.add_transformation_to(
    #                obj=page, transformation_class=TransformationRotate,
    #                arguments='{{"degrees": {}}}'.format(360 - degrees)
    #            )

    def get_absolute_url(self):
        return reverse(
            viewname='documents:document_version_view', kwargs={
                'document_version_id': self.pk
            }
        )

    def get_api_image_url(self, *args, **kwargs):
        first_page = self.pages.first()
        if first_page:
            return first_page.get_api_image_url(*args, **kwargs)

    """
    def get_intermediate_file(self):
        cache_filename = 'intermediate_file'
        cache_file = self.cache_partition.get_file(filename=cache_filename)
        if cache_file:
            logger.debug('Intermidiate file found.')
            return cache_file.open()
        else:
            logger.debug('Intermidiate file not found.')

            try:
                with self.open() as file_object:
                    converter = ConverterBase.get_converter_class()(
                        file_object=file_object
                    )
                    with converter.to_pdf() as pdf_file_object:
                        with self.cache_partition.create_file(filename=cache_filename) as file_object:
                            shutil.copyfileobj(
                                fsrc=pdf_file_object, fdst=file_object
                            )

                        return self.cache_partition.get_file(filename=cache_filename).open()
            except InvalidOfficeFormat:
                return self.open()
            except Exception as exception:
                logger.error(
                    'Error creating intermediate file "%s"; %s.',
                    cache_filename, exception
                )
                cache_file = self.cache_partition.get_file(filename=cache_filename)
                if cache_file:
                    cache_file.delete()
                raise
    """
    def get_rendered_string(self, preserve_extension=False):
        if preserve_extension:
            filename, extension = os.path.splitext(self.document.label)
            return '{} ({}){}'.format(
                filename, self.get_rendered_timestamp(), extension
            )
        else:
            return Template(
                template_string='{{ instance.document }} - {{ instance.timestamp }}'
            ).render(context={'instance': self})

    def get_rendered_timestamp(self):
        return Template(
            template_string='{{ instance.timestamp }}'
        ).render(
            context={'instance': self}
        )

    #def natural_key(self):
    #    return (self.checksum, self.document.natural_key())
    #natural_key.dependencies = ['documents.Document']

    @property
    def is_in_trash(self):
        return self.document.is_in_trash

    '''
    def open(self, raw=False):
        """
        Return a file descriptor to a document file's file irrespective of
        the storage backend
        """
        if raw:
            return self.file.storage.open(name=self.file.name)
        else:
            file_object = self.file.storage.open(name=self.file.name)

            result = DocumentFile._execute_hooks(
                hook_list=DocumentFile._pre_open_hooks,
                instance=self, file_object=file_object
            )

            if result:
                return result['file_object']
            else:
                return file_object
    '''

    @property
    def page_count(self):
        """
        The number of pages that the document posses.
        """
        return self.pages.count()

    @property
    def pages(self):
        DocumentVersionPage = apps.get_model(
            app_label='documents', model_name='DocumentVersionPage'
        )
        queryset = ModelQueryFields.get(model=DocumentVersionPage).get_queryset()
        return queryset.filter(pk__in=self.version_pages.all())

    def pages_reset(self):
        """
        Remove all page mappings and recreate them to be a 1 to 1 match
        to the latest document file.
        """
        with transaction.atomic():
            for page in self.pages.all():
                page.delete()

            for document_file_page in self.latest_file.pages.all():
                self.pages.create(
                    content_object=document_file_page
                )

    #@property
    #def pages_valid(self):
    #    DocumentVersionPage = apps.get_model(
    #        app_label='documents', model_name='DocumentVersionPage'
    #    )
    #    return self.pages.filter(pk__in=DocumentVersionPage.valid.filter(document_file=self))

    '''
    def save(self, *args, **kwargs):
        """
        Overloaded save method that updates the document file's checksum,
        mimetype, and page count when created
        """
        user = kwargs.pop('_user', None)
        new_document_version = not self.pk

        if new_document_version:
            logger.info('Creating new version for document: %s', self.document)

        try:
            with transaction.atomic():
                #self.execute_pre_save_hooks()

                #signal_mayan_pre_save.send(
                #    instance=self, sender=DocumentFile, user=user
                #)

                super().save(*args, **kwargs)

                #DocumentFile._execute_hooks(
                #    hook_list=DocumentFile._post_save_hooks,
                #    instance=self
                #)

                if new_document_file:
                    # Only do this for new documents
                    #self.update_checksum(save=False)
                    #self.update_mimetype(save=False)
                    self.save()
                    #self.update_page_count(save=False)
                    #if setting_fix_orientation.value:
                    #    self.fix_orientation()

                    logger.info(
                        'New document version "%s" created for document: %s',
                        self, self.document
                    )

                    #self.document.is_stub = False
                    #if not self.document.label:
                    #    self.document.label = force_text(self.file)

                    self.document.save(_commit_events=False)
        except Exception as exception:
            logger.error(
                'Error creating new document version for document "%s"; %s',
                self.document, exception
            )
            raise
        else:
            if new_document_version:
                pass
                #event_document_version_new.commit(
                #    actor=user, target=self, action_object=self.document
                #)
                #signal_post_file_upload.send(
                #    sender=DocumentFile, instance=self
                #)

                #if tuple(self.document.versions.all()) == (self,):
                #    signal_post_document_created.send(
                #        instance=self.document, sender=Document
                #    )
    '''
    #def save_to_file(self, file_object):
    #    """
    #    Save a copy of the document from the document storage backend
    #    to the local filesystem
    #    """
    #    with self.open() as input_file_object:
    #        shutil.copyfileobj(fsrc=input_file_object, fdst=file_object)

    #@property
    #def size(self):
    #    if self.exists():
    #        return self.file.storage.size(self.file.name)
    #    else:
    #        return None

    @property
    def uuid(self):
        # Make cache UUID a mix of document UUID, file ID
        return '{}-{}'.format(self.document.uuid, self.pk)
