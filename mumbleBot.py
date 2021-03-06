#!/usr/bin/env python3

import threading
import time
import sys
import signal
import configparser
import audioop
import subprocess as sp
import argparse
import os
import os.path
import pymumble.pymumble_py3 as pymumble
import interface
import variables as var
import hashlib
import logging
import util
import html
import base64
import requests
import numpy
import glob
import pyfluidsynth.fluidsynth as fluidsynth
from PIL import Image
from io import BytesIO
from mutagen.easyid3 import EasyID3
import re
import media.url
import media.file
import media.playlist
import media.radio
import media.system


class Future(threading.Event):
    def __init__(self):
        super().__init__()
        self.retval = None
    def set(self, value=None):
        super().set()
        self.retval = value
    def wait(self, timeout=None):
        super().wait(timeout)
        return self.retval

class MusicSourceSubprocess:
    def __init__(self, process):
        self.process = process
    def active(self):
        return self.process is not None
    def next(self):
        a = self.process.stdout.read(480)
        if a:
            a = numpy.frombuffer(a, dtype=numpy.int16)
            return (a[::2] // 2 + a[1::2] // 2).tobytes()
        return None
    def set_soundfont(self):
        pass
    def stop(self):
        self.process.kill()
        self.process = None

class MusicSourceFluidSynth:
    def __init__(self, soundfont, uri):
        content = None
        if uri.lower().startswith('https://') or uri.lower().startswith('http://'):
            r = requests.get(uri, timeout=10)
            if r.status_code == requests.codes.ok:
                content = r.content
        else:
            with open(uri, 'rb') as f:
                content = f.read()

        if content:
            args = {
                    'player.timing-source': 'sample',
                    'synth.lock-memory': 0,
                    'synth.chorus.level': 0.0,
                    'synth.reverb.level': 0.0,
                    }
            self.synth = fluidsynth.Synth(gain=0.25, samplerate=48000, **args)
            self.synth.start()
            self.sfid = self.synth.sfload(soundfont)
            self.player = fluidsynth.Player(self.synth)
            self.player.add(content)
            self.player.play()
        else:
            self.synth = None
    def active(self):
        return self.synth is not None and self.player.status() == fluidsynth.Player.PLAYING
    def next(self):
        active = self.active()
        a = self.synth.get_samples(240)
        if active and a.size > 0:
            return (a[::2] + a[1::2]).tobytes()
        return None
    def set_soundfont(self, sf):
        if self.synth:
            oldid = self.sfid
            self.synth.sfunload(oldid, True)
            self.sfid = self.synth.sfload(sf, True)
    def stop(self):
        if self.synth:
            self.player.stop()
            self.player.delete()
            self.player = None
            self.synth.delete()
            self.synth = None


class MumbleBot:
    def __init__(self, args):
        signal.signal(signal.SIGINT, self.ctrl_caught)
        self.volume = var.db.getfloat('bot', 'volume', fallback=0.5)
        self.soundfont = var.db.get('bot', 'soundfont', fallback=None)

        FORMAT = '%(asctime)s: %(message)s'
        loglevel = logging.INFO
        if args.verbose:
            loglevel = logging.DEBUG
        elif args.quiet:
            loglevel = logging.ERROR
        logfile = var.config.get('bot', 'logfile')
        if logfile:
            logging.basicConfig(filename=logfile, format=FORMAT, level=loglevel, datefmt='%Y-%m-%d %H:%M:%S')
        else:
            logging.basicConfig(format=FORMAT, level=loglevel, datefmt='%Y-%m-%d %H:%M:%S')
        if args.verbose:
            logging.debug("Starting in DEBUG loglevel")
        elif args.quiet:
            logging.error("Starting in ERROR loglevel")
        else:
            logging.info("Starting in INFO loglevel")

        var.playlist = []

        var.user = args.user
        var.music_folder = var.config.get('bot', 'music_folder')
        var.is_proxified = var.config.getboolean("webinterface", "is_web_proxified")
        self.exit = False
        self.nb_exit = 0
        self.music_source = None
        self.is_playing = False
        self.work_queue_lock = threading.Lock()
        self.work_queue = []

        if var.config.getboolean("webinterface", "enabled"):
            wi_addr = var.config.get("webinterface", "listening_addr")
            wi_port = var.config.getint("webinterface", "listening_port")
            interface.init_proxy()
            tt = threading.Thread(target=start_web_interface, args=(wi_addr, wi_port))
            tt.daemon = True
            tt.start()

        if args.host:
            host = args.host
        else:
            host = var.config.get("server", "host")

        if args.port:
            port = args.port
        else:
            port = var.config.getint("server", "port")

        if args.password:
            password = args.password
        else:
            password = var.config.get("server", "password")

        if args.channel:
            self.channel = args.channel
        else:
            self.channel = var.config.get("server", "channel")

        if args.certificate:
            certificate = args.certificate
        else:
            certificate = var.config.get("server", "certificate")

        if args.tokens:
            tokens = args.tokens
        else:
            tokens = var.config.get("server", "tokens")
            tokens = tokens.split(',')

        if args.user:
            self.username = args.user
        else:
            self.username = var.config.get("bot", "username")

        self.mumble = pymumble.Mumble(host, user=self.username, port=port, password=password, tokens=tokens,
                                      debug=var.config.getboolean('debug', 'mumbleConnection'), certfile=args.certificate)
        self.mumble.callbacks.set_callback(pymumble.constants.PYMUMBLE_CLBK_TEXTMESSAGERECEIVED, self.message_received)

        self.mumble.set_codec_profile("audio")
        self.mumble.start()  # start the mumble thread
        self.mumble.is_ready()  # wait for the connection
        self.set_comment()
        self.mumble.users.myself.unmute()  # by sure the user is not muted
        if self.channel:
            self.mumble.channels.find_by_name(self.channel).move_in()
        self.mumble.set_bandwidth(200000)

        self.loop()

    def ctrl_caught(self, signal, frame):
        logging.info("\nSIGINT caught, quitting, {} more to kill".format(2 - self.nb_exit))
        self.exit = True
        self.stop_all()
        if self.nb_exit > 1:
            logging.info("Forced Quit")
            sys.exit(0)
        self.nb_exit += 1

    def queue_work(self, func):
        future = Future()
        with self.work_queue_lock:
            self.work_queue.append(lambda: future.set(func()))
        return future

    def message_received(self, text):
        message = text.message.strip()
        user = self.mumble.users[text.actor]['name']
        if var.config.getboolean('command', 'split_username_at_space'):
            user = user.split()[0]
        if message[0] == var.config.get('command', 'command_symbol'):
            message = message[1:].split(' ', 1)
            if len(message) > 0:
                command = message[0]
                parameter = ''
                if len(message) > 1:
                    parameter = message[1]

            else:
                return

            logging.info(command + ' - ' + parameter + ' by ' + user)

            if command == var.config.get('command', 'joinme'):
                self.mumble.users.myself.move_in(self.mumble.users[text.actor]['channel_id'], token=parameter)
                return

            if not self.is_admin(user) and not var.config.getboolean('bot', 'allow_other_channel_message') and self.mumble.users[text.actor]['channel_id'] != self.mumble.users.myself['channel_id']:
                self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_in_my_channel'))
                return

            if not self.is_admin(user) and not var.config.getboolean('bot', 'allow_private_message') and text.session:
                self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'pm_not_allowed'))
                return

            for i in var.db.items("user_ban"):
                if user.lower() == i[0]:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'user_ban'))
                    return

            if command == var.config.get('command', 'user_ban'):
                if self.is_admin(user):
                    if parameter:
                        self.mumble.users[text.actor].send_text_message(util.user_ban(parameter))
                    else:
                        self.mumble.users[text.actor].send_text_message(util.get_user_ban())
                else:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_admin'))
                return

            elif command == var.config.get('command', 'user_unban'):
                if self.is_admin(user):
                    if parameter:
                        self.mumble.users[text.actor].send_text_message(util.user_unban(parameter))
                else:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_admin'))
                return

            elif command == var.config.get('command', 'url_ban'):
                if self.is_admin(user):
                    if parameter:
                        self.mumble.users[text.actor].send_text_message(util.url_ban(self.get_url_from_input(parameter)))
                    else:
                        self.mumble.users[text.actor].send_text_message(util.get_url_ban())
                else:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_admin'))
                return

            elif command == var.config.get('command', 'url_unban'):
                if self.is_admin(user):
                    if parameter:
                        self.mumble.users[text.actor].send_text_message(util.url_unban(self.get_url_from_input(parameter)))
                else:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_admin'))
                return

            if parameter:
                for i in var.db.items("url_ban"):
                    if self.get_url_from_input(parameter.lower()) == i[0]:
                        self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'url_ban'))
                        return

            if command == var.config.get('command', 'play_file') and parameter:
                music_folder = var.config.get('bot', 'music_folder')
                filenames = self.find_file(music_folder, parameter, multiple='*' in parameter)
                for filename in filenames:
                    music = {'type': 'file',
                             'path': filename,
                             'user': user,
                             'start': 0,
                             'end': 0}
                    pos = self.queue_work(lambda: (var.playlist.append(music) or len(var.playlist))).wait()
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'file_queued') % (filename, pos))

            elif command == var.config.get('command', 'play_url') and parameter:
                self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'download_in_progress') % parameter)
                entries = media.url.get_url_info(self.get_url_from_input(parameter), user)
                self.play_urls(entries, text, user)

            elif command == var.config.get('command', 'play_playlist') and parameter:
                offset = 1
                try:
                    offset = int(parameter.split(" ")[-1])
                except ValueError:
                    pass
                musics = media.playlist.get_playlist_info(url=self.get_url_from_input(parameter), start_index=offset, user=user)
                if musics:
                    self.play_urls(musics, text, user)

            elif command == var.config.get('command', 'play_radio') and parameter:
                if var.config.has_option('radio', parameter):
                    parameter = var.config.get('radio', parameter)
                music = {'type': 'radio',
                         'url': self.get_url_from_input(parameter),
                         'user': user}
                pos = self.queue_work(lambda: (var.playlist.append(music) or len(var.playlist))).wait()
                self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'file_queued') % (music['url'], pos))

            elif command == var.config.get('command', 'help'):
                self.send_msg(var.config.get('strings', 'help'))

            elif command == var.config.get('command', 'stop'):
                self.queue_work(self.stop_all)

            elif command == var.config.get('command', 'kill'):
                if self.is_admin(user):
                    self.queue_work(self.quit())
                else:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_admin'))

            elif command == var.config.get('command', 'update'):
                if self.is_admin(user):
                    self.mumble.users[text.actor].send_text_message("Starting the update")
                    tp = sp.check_output([var.config.get('bot', 'pip3_path'), 'install', '--upgrade', 'youtube-dl']).decode()
                    msg = ""
                    if "Requirement already up-to-date" in tp:
                        msg += "Youtube-dl is up-to-date"
                    else:
                        msg += "Update done : " + tp.split('Successfully installed')[1]
                    needs_restart = False
                    if 'up-to-date' not in sp.check_output(['/usr/bin/env', 'git', 'pull']).decode():
                        msg += "<br /> I'm up-to-date"
                    else:
                        msg += "<br /> I have available updates"
                        needs_restart = True
                    self.mumble.users[text.actor].send_text_message(msg)
                    if needs_restart:
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                else:
                    self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'not_admin'))

            elif command == var.config.get('command', 'stop_and_getout'):
                self.queue_work(self.stop_all).wait()
                if self.channel:
                    self.mumble.channels.find_by_name(self.channel).move_in()

            elif command == var.config.get('command', 'volume'):
                try:
                    volume = float(float(parameter) / 100)
                except ValueError:
                    volume = None

                if volume is None:
                    volume = self.queue_work(lambda: self.volume).wait()
                    self.send_msg(var.config.get('strings', 'current_volume') % float(volume * 100))
                else:
                    self.queue_work(lambda: self.set_volume(volume))
                    self.send_msg(var.config.get('strings', 'change_volume') % (
                        float(volume * 100), self.mumble.users[text.actor]['name']))

            elif command == var.config.get('command', 'soundfont'):
                if parameter:
                    sf_folder = var.config.get('bot', 'soundfont_folder')
                    filenames = self.find_file(sf_folder, parameter)
                    if len(filenames) > 0:
                        self.queue_work(lambda: self.set_soundfont(filenames[0]))
                        self.send_msg(var.config.get('strings', 'change_soundfont') % (
                            filenames[0], self.mumble.users[text.actor]['name']))
                else:
                    soundfont = self.queue_work(lambda: self.soundfont).wait()
                    self.send_msg(var.config.get('strings', 'current_soundfont') % soundfont)

            elif command == var.config.get('command', 'current_music'):
                current = self.queue_work(self.get_current_music).wait()
                if current:
                    source = current["type"]
                    if source == "radio":
                        reply = "[radio] {title} on {url} by {user}".format(
                            title=media.radio.get_radio_title(current["url"]),
                            url=current["title"],
                            user=current["user"]
                        )
                    elif source == "url" and 'from_playlist' in current:
                        reply = "[playlist] {title} (from the playlist <a href=\"{url}\">{playlist}</a> by {user}".format(
                            title=current["title"],
                            url=current["playlist_url"],
                            playlist=current["playlist_title"],
                            user=current["user"]
                        )
                    elif source == "url":
                        reply = "[url] {title} (<a href=\"{url}\">{url}</a>) by {user}".format(
                            title=current["title"],
                            url=current["url"],
                            user=current["user"]
                        )
                    elif source == "file":
                        reply = "[file] {title} by {user}".format(
                            title=current["path"],
                            user=current["user"])
                    else:
                        reply = "ERROR"
                        logging.error(current)
                else:
                    reply = var.config.get('strings', 'not_playing')

                self.send_msg(reply)

            elif command == var.config.get('command', 'skip'):
                count = 1
                if parameter is not None and parameter.isdigit() and int(parameter) > 0:
                    count = int(parameter)
                while count > 0:
                    if self.queue_work(self.next).wait():
                        count -= 1
                    else:
                        self.queue_work(self.stop_all).wait()
                        self.send_msg(var.config.get('strings', 'queue_empty'))
                        return

            elif command == var.config.get('command', 'list'):
                folder_path = var.config.get('bot', 'music_folder')

                files = self.find_file(folder_path, parameter or '*', multiple=True)
                self.print_items(files);

            elif command == var.config.get('command', 'list_soundfonts'):
                folder_path = var.config.get('bot', 'soundfont_folder')

                files = util.get_recursive_filelist_sorted(folder_path, False)
                if files:
                    self.send_msg('<br>'.join(files))
                else:
                    self.send_msg(var.config.get('strings', 'folder_empty'))

            elif command == var.config.get('command', 'midi') and parameter:
                if self.music_source and isinstance(self.music_source, MusicSourceFluidSynth):
                    player = self.music_source.player
                    midi_param = parameter.split(' ', 1)
                    midi_val = midi_param[1] if len(midi_param) > 1 else None
                    try:
                        if midi_param[0] == 'bpm':
                            if midi_val:
                                player.set_bpm(int(midi_val))
                                self.send_msg('MIDI BPM set to {}'.format(int(midi_val)))
                            else:
                                self.send_msg('MIDI BPM: {}'.format(player.bpm()))
                        elif midi_param[0] == 'tempo':
                            if midi_val:
                                player.set_midi_tempo(int(midi_val))
                                self.send_msg('MIDI Tempo set to {}'.format(int(midi_val)))
                            else:
                                self.send_msg('MIDI Tempo: {}'.format(player.midi_tempo()))
                        elif midi_param[0] == 'loop':
                            if midi_val:
                                player.set_loop(int(midi_val))
                                self.send_msg('Looping MIDI {} times'.format(int(midi_val)))
                            else:
                                self.send_msg('Usage: loop <value>')
                        else:
                            self.send_msg('invalid midi command ' + midi_param[0])
                    except ValueError:
                        self.send_msg('Not a valid integer: ' + midi_val)
                else:
                    self.send_msg('No midi playing!')


            elif command == var.config.get('command', 'queue'):
                playlist = self.queue_work(lambda: [m.copy() for m in var.playlist]).wait()
                if len(playlist) <= 1:
                    msg = var.config.get('strings', 'queue_empty')
                else:
                    msg = var.config.get('strings', 'queue_contents') + '<br />'
                    i = 1
                    for value in playlist[1:]:
                        msg += '[{}] ({}) {}<br />'.format(i, value['type'], value['title'] if 'title' in value else value['path'])
                        i += 1

                self.send_msg(msg)

            elif command == var.config.get('command', 'repeat'):
                self.queue_work(lambda: var.playlist.append(var.playlist[0]) if len(var.playlist) > 0 else None)

            elif command == var.config.get('command', 'search'):
                if str(parameter) == '':
                    self.send_msg(var.config.get('strings', 'search_error') % parameter)
                    return
                self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'search_for') % parameter)
                try:
                    entries = media.url.search(parameter, user)
                except Exception as e:
                    logging.debug(e)
                    self.send_msg(var.config.get('strings', 'search_error') % parameter)
                    return
                if len(entries) == 0:
                    self.send_msg(var.config.get('strings', 'no_search_results') % parameter)
                    return
                self.play_urls(entries, text, user)

            #else:
                #help_cmd = self.print_cmd('help')
                #self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'bad_command') % (command, help_cmd))

    def print_items(self, items):
        maxlen = self.mumble.get_max_message_length() - 1

        if len(items):
            sent = 0
            while sent < len(items):
                msg = items[sent]
                if len(msg) > maxlen:
                    msg = msg[0:maxlen-3]+'...'
                    sent += 1
                else:
                    while True:
                        sent += 1
                        if sent >= len(items):
                            break
                        nextstr = '<br>' + items[sent]
                        if len(nextstr) + len(msg) > maxlen:
                            break
                        msg += nextstr
                self.send_msg(msg)

    def print_cmd(self, cmd):
        return var.config.get('command', 'command_symbol') + var.config.get('command', cmd)

    def play_urls(self, entries, text, user):
        if entries:
            for music in entries:
                if music['duration'] > var.config.getint('bot', 'max_track_duration'):
                    self.send_msg(var.config.get('strings', 'too_long'))
                else:
                    for i in var.db.options("url_ban"):
                        if music['url'] == i:
                            self.send_msg(var.config.get('strings', 'url_ban'))
                            return
                    pos = self.queue_work(lambda: (var.playlist.append(music) or len(var.playlist))).wait()
                    if pos > 1:
                        self.mumble.users[text.actor].send_text_message(var.config.get('strings', 'file_queued') % (music['title'], pos))
        else:
            self.send_msg(var.config.get('strings', 'bad_url'))

    def find_file(self, folder, parameter, multiple=False):
        # sanitize "../" and so on
        path = os.path.abspath(os.path.join(folder, parameter or ''))
        if path.startswith(folder):
            if os.path.isfile(path):
                return [path.replace(folder, '')]
            else:
                # try to do a partial match
                if '*' in parameter:
                    matches = [file.replace(folder, '') for file in glob.glob(os.path.join(folder, '**', parameter), recursive=True)]
                    matches.sort()
                else:
                    matches = [file for file in util.get_recursive_filelist_sorted(folder, False) if parameter.lower() in file.lower()]
                if len(matches) == 0:
                    self.send_msg(var.config.get('strings', 'no_file'))
                    return []
                elif len(matches) == 1 or multiple:
                    return matches
                else:
                    msg = var.config.get('strings', 'multiple_matches') + '<br />'
                    msg += '<br />'.join(matches)
                    self.send_msg(msg)
                    return []
        else:
            self.send_msg(var.config.get('strings', 'bad_file'))
            return []

    def get_current_music(self):
        if len(var.playlist) > 0:
            return var.playlist[0].copy()
        return None

    def set_volume(self, volume):
        self.volume = volume
        var.db.set('bot', 'volume', str(volume))

    def set_soundfont(self, sf):
        self.soundfont = sf
        var.db.set('bot', 'soundfont', str(sf))
        sf_folder = var.config.get('bot', 'soundfont_folder')
        sf = os.path.join(sf_folder, sf)
        self.queue_work(lambda: self.music_source.set_soundfont(sf) if self.music_source else None)

    @staticmethod
    def is_admin(user):
        list_admin = var.config.get('bot', 'admin').split(';')
        if user in list_admin:
            return True
        else:
            return False

    def next(self):
        logging.debug("Next into the queue")
        self.stop_current()
        if len(var.playlist) > 1:
            var.playlist.pop(0)
            return True
        elif len(var.playlist) == 1:
            var.playlist.pop(0)
            return False
        else:
            return False

    def launch_music(self, music):
        uri = ""
        logging.debug("launch_music asked" + str(music))
        if music["type"] == "url":

            if 'path' not in music:
                return False

            uri = music['path']
            if os.path.isfile(uri):
                audio = EasyID3(uri)
                title = ""
                if audio["title"]:
                    title = audio["title"][0]

                path_thumbnail = music['path'][:-4] + '.jpg'  # Remove .mp3 and add .jpg
                thumbnail_html = ""
                if os.path.isfile(path_thumbnail):
                    im = Image.open(path_thumbnail)
                    im.thumbnail((100, 100), Image.ANTIALIAS)
                    buffer = BytesIO()
                    im.save(buffer, format="JPEG")
                    thumbnail_base64 = base64.b64encode(buffer.getvalue())
                    thumbnail_html = '<img src="data:image/PNG;base64,' + thumbnail_base64.decode() + '"/>'

                logging.debug("Thumbnail data " + thumbnail_html)
                if var.config.getboolean('bot', 'announce_current_music'):
                    self.send_msg(var.config.get('strings', 'now_playing') % (title, thumbnail_html))
            elif 'thumbnail' in music:
                if var.config.getboolean('bot', 'announce_current_music'):
                    title = '<a href="%s">%s</a>' % (music['url'], music['title'])
                    #thumbnail_html = '<img src="%s" width="100"/>' % music['thumbnail']
                    thumbnail_html = ""
                    self.send_msg(var.config.get('strings', 'now_playing') % (title, thumbnail_html))

        elif music["type"] == "file":
            uri = var.config.get('bot', 'music_folder') + music["path"]
            self.send_msg(var.config.get('strings', 'now_playing') % (music['path'], ""))

        elif music["type"] == "radio":
            uri = music["url"]
            title = media.radio.get_radio_server_description(uri)
            music["title"] = title
            self.send_msg(var.config.get('strings', 'now_playing') % (title or uri, ""))

        if (music['type'] == 'file' and uri.lower().endswith('.mid')) or music.get('format_id') == 'midi':
            sf_folder = var.config.get('bot', 'soundfont_folder')
            if not self.soundfont:
                self.send_msg(var.config.get('strings', 'no_soundfont') % (self.print_cmd('list_soundfonts'), self.print_cmd('soundfont')))
                return False
            soundfont = os.path.join(sf_folder, self.soundfont)
            self.music_source = MusicSourceFluidSynth(soundfont, uri)
            #command = ['fluidsynth', '-a', 'file', '-O', 's16', '-T', 'raw', '-iln', '-R', 'no', '-C', 'no', '-F', '-',
                #soundfont, uri]
            #logging.info("Fluidsynth command : " + " ".join(command))
        else:
            if var.config.getboolean('debug', 'ffmpeg'):
                ffmpeg_debug = "debug"
            else:
                ffmpeg_debug = "warning"

            command = ["ffmpeg", '-v', ffmpeg_debug, '-nostdin']
            if music["type"] != "file":
                    command += ['-reconnect', '1', '-reconnect_streamed', '1', '-reconnect_delay_max', '20']
            if music['start'] > 0:
                command += ['-ss', str(music['start'])]
            if music['end'] > 0:
                command += ['-to', str(music['end'])]
            command += ['-i', uri, '-ac', '2', '-vn', '-f', 's16le', '-ar', '48000', '-']
            logging.info("FFmpeg command : " + " ".join(command))
            self.music_source = MusicSourceSubprocess(sp.Popen(command, stdout=sp.PIPE, bufsize=480))
        self.is_playing = self.music_source.active()
        return self.is_playing

    @staticmethod
    def get_url_from_input(string):
        if string.startswith('http'):
            return string
        p = re.compile('href="(.+?)"', re.IGNORECASE)
        res = re.search(p, string)
        if res:
            return html.unescape(res.group(1))
        else:
            return False

    def loop(self):
        raw_music = None
        while not self.exit and self.mumble.isAlive():
            with self.work_queue_lock:
                queue = self.work_queue
                self.work_queue = []
            for f in queue:
                try:
                    f()
                except Exception as e:
                    print(e)

            while self.mumble.sound_output.get_buffer_size() > 0.5 and not self.exit:
                time.sleep(0.01)
            if self.music_source:
                raw_music = self.music_source.next()
                if raw_music:
                    self.mumble.sound_output.add_sound(audioop.mul(raw_music, 2, self.volume))
                else:
                    time.sleep(0.1)
            else:
                time.sleep(0.1)

            if self.music_source is None or not raw_music:
                if self.is_playing:
                    self.is_playing = False
                    self.next()
                if len(var.playlist) > 0:
                    music = var.playlist[0]
                    if music['type'] in ['radio', 'file', 'url']:
                        if not self.launch_music(music):
                            self.next()

        while self.mumble.sound_output.get_buffer_size() > 0:
            time.sleep(0.01)
        time.sleep(0.5)

        if self.exit:
            util.write_db()

    def stop_current(self):
        if self.music_source:
            self.music_source.stop()
            self.music_source = None
        self.is_playing = False

    def stop_all(self):
        self.stop_current()
        var.playlist = []

    def quit(self):
        self.stop_all()
        self.exit = True

    def set_comment(self):
        self.mumble.users.myself.comment(var.config.get('bot', 'comment'))

    def send_msg(self, msg, text=None):
        msg = msg.encode('utf-8', 'ignore').decode('utf-8')
        if not text or not text.session:
            own_channel = self.mumble.channels[self.mumble.users.myself['channel_id']]
            own_channel.send_text_message(msg)
        else:
            self.mumble.users[text.actor].send_text_message(msg)


def start_web_interface(addr, port):
    logging.info('Starting web interface on {}:{}'.format(addr, port))
    interface.web.run(port=port, host=addr)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Bot for playing music on Mumble')

    # General arguments
    parser.add_argument("--config", dest='config', type=str, default='configuration.ini', help='Load configuration from this file. Default: configuration.ini')
    parser.add_argument("--db", dest='db', type=str, default='db.ini', help='database file. Default db.ini')

    parser.add_argument("-q", "--quiet", dest="quiet", action="store_true", help="Only Error logs")
    parser.add_argument("-v", "--verbose", dest="verbose", action="store_true", help="Show debug log")

    # Mumble arguments
    parser.add_argument("-s", "--server", dest="host", type=str, help="Hostname of the Mumble server")
    parser.add_argument("-u", "--user", dest="user", type=str, help="Username for the bot")
    parser.add_argument("-P", "--password", dest="password", type=str, help="Server password, if required")
    parser.add_argument("-T", "--tokens", dest="tokens", type=str, help="Server tokens, if required")
    parser.add_argument("-p", "--port", dest="port", type=int, help="Port for the Mumble server")
    parser.add_argument("-c", "--channel", dest="channel", type=str, help="Default channel for the bot")
    parser.add_argument("-C", "--cert", dest="certificate", type=str, default=None, help="Certificate file")

    args = parser.parse_args()
    var.dbfile = args.db
    config = configparser.ConfigParser(interpolation=None, allow_no_value=True)
    parsed_configs = config.read(['configuration.default.ini', args.config], encoding='utf-8')

    db = configparser.ConfigParser(interpolation=None, allow_no_value=True)
    db.read(var.dbfile, encoding='utf-8')

    if len(parsed_configs) == 0:
        logging.error('Could not read configuration from file \"{}\"'.format(args.config), file=sys.stderr)
        sys.exit()

    var.config = config
    var.db = db
    botamusique = MumbleBot(args)
