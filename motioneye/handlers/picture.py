
# Copyright (c) 2020 Vlsarro
# Copyright (c) 2013 Calin Crisan
# This file is part of motionEye.
#
# motionEye is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import datetime
import logging
import os
import re

from tornado.ioloop import IOLoop
from tornado.web import HTTPError

from motioneye import config
from motioneye import remote
from motioneye import mediafiles
from motioneye import monitor
from motioneye import motionctl
from motioneye import mjpgclient
from motioneye import settings
from motioneye import utils
from motioneye.handlers.base import BaseHandler


__all__ = ('PictureHandler',)


class PictureHandler(BaseHandler):
    def compute_etag(self):
        return None

    async def get(self, camera_id, op, filename=None, group=None):
        if camera_id is not None:
            camera_id = int(camera_id)
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')

        if op == 'current':
            self.current(camera_id)

        elif op == 'list':
            self.list(camera_id)

        elif op == 'frame':
            self.frame(camera_id)

        elif op == 'download':
            self.download(camera_id, filename)

        elif op == 'preview':
            self.preview(camera_id, filename)

        elif op == 'zipped':
            self.zipped(camera_id, group)

        elif op == 'timelapse':
            self.timelapse(camera_id, group)

        else:
            raise HTTPError(400, 'unknown operation')

    async def post(self, camera_id, op, filename=None, group=None):
        if group == '/':  # ungrouped
            group = ''

        if camera_id is not None:
            camera_id = int(camera_id)
            if camera_id not in config.get_camera_ids():
                raise HTTPError(404, 'no such camera')

        if op == 'delete':
            self.delete(camera_id, filename)

        elif op == 'delete_all':
            self.delete_all(camera_id, group)

        else:
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth(prompt=False)
    def current(self, camera_id, retry=0):
        self.set_header('Content-Type', 'image/jpeg')
        self.set_header('Cache-Control', 'no-store, must-revalidate')
        self.set_header('Pragma', 'no-cache')
        self.set_header('Expires', '0')

        width = self.get_argument('width', None)
        height = self.get_argument('height', None)

        width = width and float(width)
        height = height and float(height)

        camera_id_str = str(camera_id)

        camera_config = config.get_camera(camera_id)
        if utils.is_local_motion_camera(camera_config):
            picture = mediafiles.get_current_picture(camera_config, width=width, height=height)

            # picture is not available usually when the corresponding internal mjpeg client has been closed;
            # get_current_picture() will make sure to start a client, but a jpeg frame is not available right away;
            # wait at most 5 seconds and retry every 200 ms.
            if not picture and retry < 25:
                return IOLoop.instance().add_timeout(datetime.timedelta(seconds=0.2), self.current,
                                                     camera_id=camera_id, retry=retry + 1)

            self.set_cookie('motion_detected_' + camera_id_str, str(motionctl.is_motion_detected(camera_id)).lower())
            self.set_cookie('capture_fps_' + camera_id_str, '%.1f' % mjpgclient.get_fps(camera_id))
            self.set_cookie('monitor_info_' + camera_id_str, monitor.get_monitor_info(camera_id))

            self.try_finish(picture)

        elif utils.is_remote_camera(camera_config):
            def on_response(motion_detected=False, capture_fps=None, monitor_info=None, picture=None, error=None):
                if error:
                    return self.try_finish(None)

                self.set_cookie('motion_detected_' + camera_id_str, str(motion_detected).lower())
                self.set_cookie('capture_fps_' + camera_id_str, '%.1f' % capture_fps)
                self.set_cookie('monitor_info_' + camera_id_str, monitor_info or '')

                self.try_finish(picture)

            remote.get_current_picture(camera_config, width=width, height=height, callback=on_response)

        else:  # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def list(self, camera_id):
        logging.debug('listing pictures for camera %(id)s' % {'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.is_local_motion_camera(camera_config):
            def on_media_list(media_list):
                if media_list is None:
                    return self.finish_json({'error': 'Failed to get pictures list.'})

                self.finish_json({
                    'mediaList': media_list,
                    'cameraName': camera_config['camera_name']
                })

            mediafiles.list_media(camera_config, media_type='picture',
                                  callback=on_media_list, prefix=self.get_argument('prefix', None))

        elif utils.is_remote_camera(camera_config):
            def on_response(remote_list=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to get picture list for %(url)s: %(msg)s.' % {
                        'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json(remote_list)

            remote.list_media(camera_config, media_type='picture', prefix=self.get_argument('prefix', None),
                              callback=on_response)

        else:  # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    def frame(self, camera_id):
        camera_config = config.get_camera(camera_id)

        if (utils.is_local_motion_camera(camera_config) or
                utils.is_simple_mjpeg_camera(camera_config) or
                self.get_argument('title', None) is not None):

            self.render('main.html',
                        frame=True,
                        camera_id=camera_id,
                        camera_config=camera_config,
                        title=self.get_argument('title', camera_config.get('camera_name', '')),
                        admin_username=config.get_main().get('@admin_username'),
                        static_path='../../../static/')

        elif utils.is_remote_camera(camera_config):
            def on_response(remote_ui_config=None, error=None):
                if error:
                    return self.render('main.html',
                                       frame=True,
                                       camera_id=camera_id,
                                       camera_config=camera_config,
                                       title=self.get_argument('title', ''))

                # issue a fake motion_camera_ui_to_dict() call to transform
                # the remote UI values into motion config directives
                remote_config = config.motion_camera_ui_to_dict(remote_ui_config)

                self.render('main.html',
                            frame=True,
                            camera_id=camera_id,
                            camera_config=remote_config,
                            title=self.get_argument('title', remote_config['camera_name']),
                            admin_username=config.get_main().get('@admin_username'))

            remote.get_config(camera_config, on_response)

    @BaseHandler.auth()
    def download(self, camera_id, filename):
        logging.debug('downloading picture %(filename)s of camera %(id)s' % {
            'filename': filename, 'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.is_local_motion_camera(camera_config):
            content = mediafiles.get_media_content(camera_config, filename, 'picture')

            pretty_filename = camera_config['camera_name'] + '_' + os.path.basename(filename)
            self.set_header('Content-Type', 'image/jpeg')
            self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')

            self.finish(content)

        elif utils.is_remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to download picture from %(url)s: %(msg)s.' % {
                        'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                pretty_filename = os.path.basename(filename)  # no camera name available w/o additional request
                self.set_header('Content-Type', 'image/jpeg')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')

                self.finish(response)

            remote.get_media_content(camera_config, filename=filename, media_type='picture', callback=on_response)

        else:  # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def preview(self, camera_id, filename):
        logging.debug('previewing picture %(filename)s of camera %(id)s' % {
            'filename': filename, 'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.is_local_motion_camera(camera_config):
            content = mediafiles.get_media_preview(camera_config, filename, 'picture',
                                                   width=self.get_argument('width', None),
                                                   height=self.get_argument('height', None))

            if content:
                self.set_header('Content-Type', 'image/jpeg')

            else:
                self.set_header('Content-Type', 'image/svg+xml')
                content = open(os.path.join(settings.STATIC_PATH, 'img', 'no-preview.svg')).read()

            self.finish(content)

        elif utils.is_remote_camera(camera_config):
            def on_response(content=None, error=None):
                if content:
                    self.set_header('Content-Type', 'image/jpeg')

                else:
                    self.set_header('Content-Type', 'image/svg+xml')
                    content = open(os.path.join(settings.STATIC_PATH, 'img', 'no-preview.svg')).read()

                self.finish(content)

            remote.get_media_preview(camera_config, filename=filename, media_type='picture',
                                     width=self.get_argument('width', None),
                                     height=self.get_argument('height', None),
                                     callback=on_response)

        else:  # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth(admin=True)
    def delete(self, camera_id, filename):
        logging.debug('deleting picture %(filename)s of camera %(id)s' % {
            'filename': filename, 'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.is_local_motion_camera(camera_config):
            try:
                mediafiles.del_media_content(camera_config, filename, 'picture')
                self.finish_json()

            except Exception as e:
                self.finish_json({'error': str(e)})

        elif utils.is_remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to delete picture from %(url)s: %(msg)s.' % {
                        'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json()

            remote.del_media_content(camera_config, filename=filename, media_type='picture', callback=on_response)

        else:  # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def zipped(self, camera_id, group):
        key = self.get_argument('key', None)
        camera_config = config.get_camera(camera_id)

        if key:
            logging.debug('serving zip file for group "%(group)s" of camera %(id)s with key %(key)s' % {
                'group': group or 'ungrouped', 'id': camera_id, 'key': key})

            if utils.is_local_motion_camera(camera_config):
                data = mediafiles.get_prepared_cache(key)
                if not data:
                    logging.error('prepared cache data for key "%s" does not exist' % key)

                    raise HTTPError(404, 'no such key')

                pretty_filename = camera_config['camera_name'] + '_' + group
                pretty_filename = re.sub('[^a-zA-Z0-9]', '_', pretty_filename)

                self.set_header('Content-Type', 'application/zip')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + '.zip;')
                self.finish(data)

            elif utils.is_remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to download zip file from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    self.set_header('Content-Type', response['content_type'])
                    self.set_header('Content-Disposition', response['content_disposition'])
                    self.finish(response['data'])

                remote.get_zipped_content(camera_config, media_type='picture', key=key, group=group,
                                          callback=on_response)

            else:  # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

        else:  # prepare
            logging.debug('preparing zip file for group "%(group)s" of camera %(id)s' % {
                'group': group or 'ungrouped', 'id': camera_id})

            if utils.is_local_motion_camera(camera_config):
                def on_zip(data):
                    if data is None:
                        return self.finish_json({'error': 'Failed to create zip file.'})

                    key = mediafiles.set_prepared_cache(data)
                    logging.debug('prepared zip file for group "%(group)s" of camera %(id)s with key %(key)s' % {
                        'group': group or 'ungrouped', 'id': camera_id, 'key': key})
                    self.finish_json({'key': key})

                mediafiles.get_zipped_content(camera_config, media_type='picture', group=group, callback=on_zip)

            elif utils.is_remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to make zip file at %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    self.finish_json({'key': response['key']})

                remote.make_zipped_content(camera_config, media_type='picture', group=group, callback=on_response)

            else:  # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth()
    def timelapse(self, camera_id, group):
        key = self.get_argument('key', None)
        check = self.get_argument('check', False)
        camera_config = config.get_camera(camera_id)

        if key:  # download
            logging.debug('serving timelapse movie for group "%(group)s" of camera %(id)s with key %(key)s' % {
                'group': group or 'ungrouped', 'id': camera_id, 'key': key})

            if utils.is_local_motion_camera(camera_config):
                data = mediafiles.get_prepared_cache(key)
                if data is None:
                    logging.error('prepared cache data for key "%s" does not exist' % key)

                    raise HTTPError(404, 'no such key')

                pretty_filename = camera_config['camera_name'] + '_' + group
                pretty_filename = re.sub('[^a-zA-Z0-9]', '_', pretty_filename)
                pretty_filename += '.' + mediafiles.FFMPEG_EXT_MAPPING.get(camera_config['movie_codec'], 'avi')

                self.set_header('Content-Type', 'video/x-msvideo')
                self.set_header('Content-Disposition', 'attachment; filename=' + pretty_filename + ';')
                self.finish(data)

            elif utils.is_remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        msg = 'Failed to download timelapse movie from %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}

                        return self.finish_json({'error': msg})

                    self.set_header('Content-Type', response['content_type'])
                    self.set_header('Content-Disposition', response['content_disposition'])
                    self.finish(response['data'])

                remote.get_timelapse_movie(camera_config, key, group=group, callback=on_response)

            else:  # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

        elif check:
            logging.debug('checking timelapse movie status for group "%(group)s" of camera %(id)s' % {
                'group': group or 'ungrouped', 'id': camera_id})

            if utils.is_local_motion_camera(camera_config):
                status = mediafiles.check_timelapse_movie()
                if status['progress'] == -1 and status['data']:
                    key = mediafiles.set_prepared_cache(status['data'])
                    logging.debug('prepared timelapse movie for group "%(group)s" of camera %(id)s with key %(key)s' % {
                        'group': group or 'ungrouped', 'id': camera_id, 'key': key})
                    self.finish_json({'key': key, 'progress': -1})

                else:
                    self.finish_json(status)

            elif utils.is_remote_camera(camera_config):
                def on_response(response=None, error=None):
                    if error:
                        msg = 'Failed to check timelapse movie progress at %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}

                        return self.finish_json({'error': msg})

                    if response['progress'] == -1 and response.get('key'):
                        self.finish_json({'key': response['key'], 'progress': -1})

                    else:
                        self.finish_json(response)

                remote.check_timelapse_movie(camera_config, group=group, callback=on_response)

            else:  # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

        else:  # start timelapse
            interval = int(self.get_argument('interval'))
            framerate = int(self.get_argument('framerate'))

            msg = 'preparing timelapse movie for group "%(group)s" of camera %(id)s with rate %(framerate)s/%(int)s' % {
                'group': group or 'ungrouped', 'id': camera_id, 'framerate': framerate, 'int': interval}
            logging.debug(msg)

            if utils.is_local_motion_camera(camera_config):
                status = mediafiles.check_timelapse_movie()
                if status['progress'] != -1:
                    self.finish_json({'progress': status['progress']})  # timelapse already active

                else:
                    mediafiles.make_timelapse_movie(camera_config, framerate, interval, group=group)
                    self.finish_json({'progress': -1})

            elif utils.is_remote_camera(camera_config):
                def on_status(response=None, error=None):
                    if error:
                        return self.finish_json({'error': 'Failed to make timelapse movie at %(url)s: %(msg)s.' % {
                            'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                    if response['progress'] != -1:
                        return self.finish_json({'progress': response['progress']})  # timelapse already active

                    def on_make(response=None, error=None):
                        if error:
                            return self.finish_json({'error': 'Failed to make timelapse movie at %(url)s: %(msg)s.' % {
                                'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                        self.finish_json({'progress': -1})

                    remote.make_timelapse_movie(camera_config, framerate, interval, group=group, callback=on_make)

                remote.check_timelapse_movie(camera_config, group=group, callback=on_status)

            else:  # assuming simple mjpeg camera
                raise HTTPError(400, 'unknown operation')

    @BaseHandler.auth(admin=True)
    def delete_all(self, camera_id, group):
        logging.debug('deleting picture group "%(group)s" of camera %(id)s' % {
            'group': group or 'ungrouped', 'id': camera_id})

        camera_config = config.get_camera(camera_id)
        if utils.is_local_motion_camera(camera_config):
            try:
                mediafiles.del_media_group(camera_config, group, 'picture')
                self.finish_json()

            except Exception as e:
                self.finish_json({'error': str(e)})

        elif utils.is_remote_camera(camera_config):
            def on_response(response=None, error=None):
                if error:
                    return self.finish_json({'error': 'Failed to delete picture group at %(url)s: %(msg)s.' % {
                        'url': remote.pretty_camera_url(camera_config), 'msg': error}})

                self.finish_json()

            remote.del_media_group(camera_config, group=group, media_type='picture', callback=on_response)

        else:  # assuming simple mjpeg camera
            raise HTTPError(400, 'unknown operation')

    def try_finish(self, content):
        try:
            self.finish(content)

        except IOError as e:
            logging.warning('could not write response: %(msg)s' % {'msg': str(e)})
