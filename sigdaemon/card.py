#!/usr/bin/env python2.7

import sys
import zmq
import argparse
import logging
import subprocess
import datetime
import mpd


# ZeroMQ publisher socket
SOCK = "tcp://localhost:9090"

# How many seconds to keep the system alive during loss of ignition power
SHUTDOWN_SECONDS = 3

# Delta MPD volume each time a volume change event is triggered
VOLUME_STEP = 2

# Repeat interval for repeatable commands, in milliseconds
TICK = 100


def repeatable(func):
    """
    Decorator that specifies that an event should be repeated at the next tick
    """
    def func_wrapper(class_):
        func(class_)
        return lambda: func(class_)
    return func_wrapper


class System(object):
    """
    Trigger system events such as run, shutdown, etc.
    """
    def run(self, cmd):
        logging.info("Running shell command: %s" % ' '.join(cmd))
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        exit_code = process.returncode
        return exit_code, stderr, stdout

    def shutdown(self):
        logging.info("Shutting down the entire system!")
        self.run(['/sbin/init', '0'])

    def try_shutdown(self):
        logging.info("Checking whether system should be shut down...")
        if pending_shutdown:
            logging.info("Yes!")
            shutdown()
            return True
        logging.info("No.")
        return False

    def android_toggle_screen(self):
        logging.info("Toggling Android screen on/off")
        self.run(['/usr/local/bin/adb', 'shell', 'input', 'keyevent', '26'])


class MPD(object):
    """
    Communicate with the Music Player Daemon
    """
    def __init__(self, mpd_client):
        self.mpd_client = mpd_client

    def connect(self):
        self.mpd_client.timeout = 5
        self.mpd_client.idletimeout = None
        self.mpd_client.connect('localhost', 6600)

    def get_status(self):
        try:
            status = self.mpd_client.status()
        except mpd.ConnectionError:
            self.connect()
            status = self.mpd_client.status()
        return status

    def prev(self):
        status = self.get_status()
        logging.info("Switching to previous song")
        self.mpd_client.previous()

    def next(self):
        status = self.get_status()
        logging.info("Switching to next song")
        self.mpd_client.next()

    def play_or_pause(self):
        status = self.get_status()
        if status['state'] == 'stop':
            logging.info("Starting to play music")
            self.mpd_client.play()
        else:
            logging.info("Toggling play/pause state")
            self.mpd_client.pause()

    def volume_shift(self, increment):
        status = self.get_status()
        shifted = int(status['volume']) + increment
        if shifted > 100:
            shifted = 100
        if shifted < 0:
            shifted = 0
        logging.info("Setting volume to %d" % shifted)
        self.mpd_client.setvol(shifted)


class Dispatcher(object):
    """
    Map button events to correct actions
    """
    def __init__(self, system, mpd):
        self.system = system
        self.mpd = mpd
        self.power = True  # assume power on at boot
        self.power_on_time = None
        self.set_power_on_time()
        self.button_mode = 'neutral'
        self.mode_dispatched = True
        self.is_shutting_down = True

    def set_power_on_time(self):
        self.power_on_time = datetime.datetime.now()
        self.is_shutting_down = False

    def needs_shutdown(self):
        if self.power or self.is_shutting_down:
            return False
        delta = datetime.datetime.now() - self.power_on_time
        return delta.total_seconds() > SHUTDOWN_SECONDS

    def shutdown(self):
        self.is_shutting_down = True
        self.system.shutdown()

    #
    # Rotary events
    #
    def neutral_rotary_left(self):
        self.mpd.volume_shift(-VOLUME_STEP)

    def mode_rotary_left(self):
        self.mpd.prev()

    def neutral_rotary_right(self):
        self.mpd.volume_shift(VOLUME_STEP)

    def mode_rotary_right(self):
        self.mpd.next()

    def neutral_rotary_press(self):
        self.mpd.play_or_pause()

    def mode_rotary_press(self):
        self.system.android_toggle_screen()

    #
    # Steering wheel up/down and volumes
    #
    @repeatable
    def neutral_volume_down_press(self):
        self.mpd.volume_shift(-VOLUME_STEP)

    @repeatable
    def neutral_volume_up_press(self):
        self.mpd.volume_shift(VOLUME_STEP)

    def neutral_arrow_up_press(self):
        self.mpd.next()

    def neutral_arrow_down_press(self):
        self.mpd.prev()

    #
    # Ignition power events
    #
    def neutral_power_on(self):
        logging.info("Ignition power has been restored, system will remain active.")
        self.power = True
        self.set_power_on_time()

    def neutral_power_off(self):
        logging.info("Ignition power has been lost, shutting down in %d seconds unless power is restored..." % SHUTDOWN_SECONDS)
        self.power = False
        self.set_power_on_time()

    def mode_power_on(self):
        return self.neutral_power_on()

    def mode_power_off(self):
        """
        Shutting down the power when the MODE key is held down prevents the
        hardware from being turned off at all.
        """
        logging.info("MODE key held down when turning off power, system is NOT shutting down.")
        self.power = True
        self.set_power_on_time()

    def neutral_mode_press(self):
        self.button_mode = 'mode'

    def mode_mode_depress(self):
        self.button_mode = 'neutral'
        if not self.mode_dispatched:
            self.system.android_toggle_screen()

    #
    # Dispatch message arrays to functions
    #
    def dispatch(self, *args):
        ret = None
        func = self.get_dispatch_function(*args)

        set_dispatched = (self.button_mode == 'mode' and args[0] != 'mode')
        if callable(func):
            logging.debug("Dispatching to '%s'" % str(func.__name__))
            ret = func()
        else:
            logging.warn("No dispatcher found for this event")

        self.mode_dispatched = set_dispatched
        logging.debug("Setting mode_dispatched variable to %s" % set_dispatched)

        return ret

    def get_dispatch_function(self, *args):
        """
        Figure out which dispatcher function to use
        """
        pattern = "%s_%s"
        base_name = '_'.join(args)
        func_name = pattern % (self.button_mode, base_name)
        logging.debug("Looking for function name 'Dispatcher.%s'" % func_name)
        func = getattr(self, func_name, None)
        return func


class Card(object):
    """
    Main loop and ZeroMQ communications
    """
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.repeat_func = None

    def setup_zeromq(self):
        logging.info("Setting up ZeroMQ socket...")
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.SUB)
        self.socket.connect(SOCK)
        self.socket.setsockopt_string(zmq.SUBSCRIBE, u'')
        self.poller = zmq.Poller()
        self.poller.register(self.socket, zmq.POLLIN)
        logging.info("Listening for events from signal daemon on %s" % SOCK)

    def process_zeromq_message(self):
        string = self.socket.recv_string()
        logging.info("Received event '%s'" % string)
        tokens = string.strip().lower().split()
        self.repeat_func = self.dispatcher.dispatch(*tokens)

    def run_tick(self):
        if self.dispatcher.needs_shutdown():
            self.dispatcher.shutdown()
        if callable(self.repeat_func):
            logging.info("Repeating last dispatched function")
            self.repeat_func()

    def run(self):
        while True:
            events = dict(self.poller.poll(TICK))
            if events:
                self.process_zeromq_message()
            else:
                self.run_tick()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', help='Enable debug output', action='store_true')
    args = parser.parse_args()

    loglevel = logging.DEBUG if args.debug else logging.INFO

    logging.basicConfig(level=loglevel, format='%(asctime)s %(levelname)s %(message)s')

    mpdclient = mpd.MPDClient()
    mpd_ = MPD(mpdclient)
    system = System()
    dispatcher = Dispatcher(system, mpd_)

    card = Card(dispatcher)
    card.setup_zeromq()

    try:
        card.run()

    except KeyboardInterrupt:
        pass
