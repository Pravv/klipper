# Support for ADS1100 ADC chip connected via I2C
#
# Copyright (C) 2022 Martin Hierholzer <martin@hierholzer.info>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, struct
from . import bus

ADS1100_CHIP_ADDR = 0x49
ADS1100_I2C_SPEED = 3000000

ADS1100_SAMPLE_RATE_TABLE = {8: 3, 16: 2, 32: 1, 128: 0}
ADS1100_MAXVALUE_BY_RATE_TABLE = {8: 32768, 16: 16384, 32: 8192, 128: 2048}
ADS1100_GAIN_TABLE = {1: 0, 2: 1, 4: 2, 8: 3}


class ADS1100Error(Exception):
    pass


class MCU_ADS1100:
    def __init__(self, config):
        self._printer = config.get_printer()
        self._reactor = self._printer.get_reactor()
        self._name = config.get_name().split()[1]
        self._i2c = bus.MCU_I2C_from_config(config,
                                            default_addr=ADS1100_CHIP_ADDR, default_speed=ADS1100_I2C_SPEED)
        self._mcu = self._i2c.get_mcu()
        self._gain = config.getint('gain', 1, minval=1)
        if self._gain not in ADS1100_GAIN_TABLE:
            raise self._printer.config_error("ADS1100 does not support the "
                                            "selected gain: %d" % self._gain)
        # Register setup_pin
        ppins = self._printer.lookup_object('pins')
        ppins.register_chip(self._name, self)

        self._last_value = 0.
        self._last_time = 0
        self._value = 0.
        self._state = 0
        self._error_count = 0

        self._rate = 0
        self._norm = 0

        self._sample_time = 0
        self._sample_count = 0
        self._minval = 0
        self._maxval = 0
        self._range_check_count = 0

        self._sample_timer = None
        self._callback = None
        self._report_time = 0
        self._last_callback_time = 0


        query_adc = self._printer.lookup_object('query_adc')
        query_adc.register_adc(self._name, self)

        self._mcu.register_config_callback(self._build_config)
        self._printer.register_event_handler("klippy:ready", self._handle_ready)

        self.register_commands(self._name)
        self.setup_minmax(0.03,15)


    def get_mcu(self):
        return self._mcu

    def register_commands(self, name):
        logging.info('registering commands: %s', (name))
        # Register commands
        gcode = self._printer.lookup_object('gcode')
        gcode.register_mux_command("TEST_ADC", "CHIP", name,
                                   self.cmd_ACCELEROMETER_MEASURE,
                                   desc=self.cmd_ACCELEROMETER_MEASURE_help)
    cmd_ACCELEROMETER_MEASURE_help = "Start/stop accelerometer"
    def cmd_ACCELEROMETER_MEASURE(self, gcmd):
        gcmd.respond_info("Writing raw accelerometer data to %s file"
                          % (self.get_last_value()[0],))

    def setup_minmax(self, sample_time, sample_count,
                     minval=-1., maxval=1., range_check_count=0):
        self._sample_time = sample_time
        self._sample_count = sample_count
        self._minval = minval
        self._maxval = maxval
        self._range_check_count = range_check_count

    def setup_adc_callback(self, report_time, callback):
        self._report_time = report_time
        self._callback = callback

    def get_last_value(self):
        return self._last_value, self._last_time

    def _build_config(self):
        logging.info('_build_config')
        if not self._sample_count:
            return

        # choose closest possible conversion rate
        rate = 1. / self._sample_time
        if rate < (8 + 16) / 2:
            rate = 8
        elif rate < (16 + 32) / 2:
            rate = 16
        elif rate < (32 + 128) / 2:
            rate = 32
        else:
            rate = 128
        self._rate = rate

        # store corrected sample time (used to setup readout timer)
        self._sample_time = 1. / rate

        # store normalisation matching the chosen rate
        self._norm = float(ADS1100_MAXVALUE_BY_RATE_TABLE[rate])

    def _handle_ready(self):
        logging.info('_handle_ready')
        # configuration byte: continuous conversion (SC bit not set), selected
        # gain and SPS
        config = ADS1100_SAMPLE_RATE_TABLE[self._rate] << 2 \
                 | ADS1100_GAIN_TABLE[self._gain]

        # write the 8 bit configuration register
        self._i2c.i2c_write([config])

        # setup readout timer
        self._sample_timer = self._reactor.register_timer(self._handle_timer,
                                                          self._reactor.NOW)

    def _read_response(self):
        while True:
            # read with error handling, spurious errors are possible
            result = self._i2c.i2c_read([], 2)
            response = bytearray(result['response'])

            # retry if response too short
            if len(response) < 2:
                logging.info("ADS1100: conversion failed, trying again...")
                continue

            # return response
            self._conversion_started = False
            return (response, result['#receive_time'])

    def _handle_timer(self, eventtime):
        (response, receive_time) = self._read_response()
        self._value += struct.unpack('>h', response[0:2])[0]
        self._state += 1
        if self._state < self._sample_count:
            return eventtime + self._sample_time

        self._last_value = self._value / self._sample_count / self._norm
        self._last_time = receive_time

        self._state = 0
        self._value = 0.

        if self._last_value < self._minval or self._last_value > self._maxval:
            self._error_count += 1
            if self._error_count >= self._range_check_count:
                self._printer.invoke_shutdown("ADC out of range")
        else:
            self._error_count = 0

        if self._callback is not None:
            if eventtime >= self._last_callback_time + self._report_time:
                self._last_callback_time = eventtime
                self._callback(self._mcu.estimated_print_time(self._last_time),
                               self._last_value)

        return eventtime + self._sample_time




def load_config_prefix(config):
    logging.info('load_config_prefix')
    return MCU_ADS1100(config)
def load_config(config):
    logging.info('load_config')
    return MCU_ADS1100(config)