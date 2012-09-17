# -*- coding: utf-8 -*-
# Copyright (C) 2007-2010 Samalyse SARL
# Copyright (C) 2010-2012 Parisson SARL

# This software is a computer program whose purpose is to backup, analyse,
# transcode and stream any audio content with its metadata over a web frontend.

# This software is governed by the CeCILL  license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".

# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.

# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.

# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL license and that you accept its terms.

# Authors: Olivier Guilyardi <olivier@samalyse.com>
#          Guillaume Pellerin <yomguy@parisson.com>

import re
import os
import sys
import csv
import time
import random
import datetime
import timeside

from jsonrpc import jsonrpc_method

from django.utils.decorators import method_decorator
from django.contrib.auth import authenticate, login
from django.template import RequestContext, loader
from django import template
from django.http import HttpResponse, HttpResponseRedirect
from django.http import Http404
from django.shortcuts import render_to_response, redirect, get_object_or_404
from django.views.generic import list_detail
from django.views.generic import DetailView
from django.conf import settings
from django.contrib import auth
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.context_processors import csrf
from django.forms.models import modelformset_factory, inlineformset_factory
from django.contrib.auth.models import User
from django.utils.translation import ugettext
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import ObjectDoesNotExist
from django.contrib.syndication.views import Feed
from django.core.servers.basehttp import FileWrapper

from telemeta.models import *
import telemeta.models
import telemeta.interop.oai as oai
from telemeta.interop.oaidatasource import TelemetaOAIDataSource
from telemeta.util.unaccent import unaccent
from telemeta.util.unaccent import unaccent_icmp
from telemeta.util.logger import Logger
from telemeta.util.unicode import UnicodeWriter
from telemeta.cache import TelemetaCache
import pages
from telemeta.forms import *

# Model type definition
mods = {'item': MediaItem, 'collection': MediaCollection,
        'corpus': MediaCorpus, 'fonds': MediaFonds, 'marker': MediaItemMarker, }

# TOOLS

class FixedFileWrapper(FileWrapper):
    def __iter__(self):
        self.filelike.seek(0)
        return self

def send_file(request, filename, content_type='image/jpeg'):
    """
    Send a file through Django without loading the whole file into
    memory at once. The FileWrapper will turn the file object into an
    iterator for chunks of 8KB.
    """
    wrapper = FixedFileWrapper(file(filename, 'rb'))
    response = HttpResponse(wrapper, content_type=content_type)
    response['Content-Length'] = os.path.getsize(filename)
    return response

def render(request, template, data = None, mimetype = None):
    return render_to_response(template, data, context_instance=RequestContext(request),
                              mimetype=mimetype)

def stream_from_processor(__decoder, __processor, __flag, metadata=None):
    while True:
        __frames, __eodproc = __processor.process(*__decoder.process())
        if __eodproc or not len(__frames):
            if metadata:
                __processor.set_metadata(metadata)
                __processor.write_metadata()
            __flag.value = True
            __flag.save()
            break
        yield __processor.chunk

def stream_from_file(__file):
    chunk_size = 0x100000
    f = open(__file, 'r')
    while True:
        __chunk = f.read(chunk_size)
        if not len(__chunk):
            f.close()
            break
        yield __chunk

def get_public_access(access, year_from=None, year_to=None):
    # Rolling publishing date : public access is given when time between recorded year
    # and current year is over the settings value PUBLIC_ACCESS_PERIOD
    if year_from and not year_from == 0:
        year = year_from
    elif year_to and not year_to == 0:
        year = year_to
    else:
        year = 0
    if access == 'full':
        public_access = True
    else:
        public_access = False
        if year and not year == 'None':
            year_now = datetime.datetime.now().strftime("%Y")
            if int(year_now) - int(year) >= settings.TELEMETA_PUBLIC_ACCESS_PERIOD:
                public_access = True
        else:
            public_access = False
    return public_access

def get_revisions(nb, user=None):
    last_revisions = Revision.objects.order_by('-time')
    if user:
        last_revisions = last_revisions.filter(user=user)
    last_revisions = last_revisions[0:nb]
    revisions = []

    for revision in last_revisions:
        for type in mods.keys():
            if revision.element_type == type:
                try:
                    element = mods[type].objects.get(pk=revision.element_id)
                except:
                    element = None
        if not element == None:
            revisions.append({'revision': revision, 'element': element})
    return revisions

def get_playlists(request, user=None):
    if not user:
        user = request.user
    playlists = []
    if user.is_authenticated():
        user_playlists = Playlist.objects.filter(author=user)
        for playlist in user_playlists:
            playlist_resources = PlaylistResource.objects.filter(playlist=playlist)
            resources = []
            for resource in playlist_resources:
                try:
                    for type in mods.keys():
                        if resource.resource_type == type:
                            element = mods[type].objects.get(id=resource.resource_id)
                except:
                    element = None
                resources.append({'element': element, 'type': resource.resource_type, 'public_id': resource.public_id })
            playlists.append({'playlist': playlist, 'resources': resources})
    return playlists

def check_related_media(medias):
    for media in medias:
        if not media.mime_type:
            media.set_mime_type()
            media.save()
        if not media.title and media.url:
            if 'https' in media.url:
                media.url = media.url.replace('https', 'http')
            import lxml.etree
            parser = lxml.etree.HTMLParser()
            tree = lxml.etree.parse(media.url, parser)
            try:
                title = tree.find(".//title").text
            except:
                title = media.url
            media.title = title.replace('\n', '').strip()
            media.save()

def auto_code(resources, base_code):
    index = 1
    while True:
        code = base_code + '_' + str(index)
        r = resources.filter(code=code)
        if not r:
            break
        index += 1
    return code

