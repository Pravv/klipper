# Support for ADS1100 ADC chip connected via I2C
#
# Copyright (C) 2022 Martin Hierholzer <martin@hierholzer.info>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import logging, struct
from . import bus

ADS1100_CHIP_ADDR = 0x48
ADS1100_I2C_SPEED = 3000000

ADS1100_SAMPLE_RATE_TABLE = {8: 3, 16: 2, 32: 1, 128: 0}
ADS1100_MAXVALUE_BY_RATE_TABLE = {8: 32768, 16: 16384, 32: 8192, 128: 2048}
ADS1100_GAIN_TABLE = {1: 0, 2: 1, 4: 2, 8: 3}


class ADS1100Error(Exception):
    pass

class SoftwareI2C:
    def __init__(self, config, addr):
        self.addr = addr << 1
        self.update_pin_cmd = None
        # Lookup pins
        ppins = config.get_printer().lookup_object('pins')
        scl_pin = config.get('scl_pin')
        scl_params = ppins.lookup_pin(scl_pin, share_type='sw_scl')
        self.mcu = scl_params['chip']
        self.scl_pin = scl_params['pin']
        self.scl_main = scl_params.get('class')
        if self.scl_main is None:
            self.scl_main = scl_params['class'] = self
            self.scl_oid = self.mcu.create_oid()
            self.cmd_queue = self.mcu.alloc_command_queue()
            self.mcu.register_config_callback(self.build_config)
        else:
            self.scl_oid = self.scl_main.scl_oid
            self.cmd_queue = self.scl_main.cmd_queue
        sda_params = ppins.lookup_pin(config.get('sda_pin'))
        self.sda_oid = self.mcu.create_oid()
        if sda_params['chip'] != self.mcu:
            raise ppins.error("%s: scl_pin and sda_pin must be on same mcu" % (
                config.get_name(),))
        self.mcu.add_config_cmd("config_digital_out oid=%d pin=%s"
                                " value=%d default_value=%d max_duration=%d" % (
                                    self.sda_oid, sda_params['pin'], 1, 1, 0))
    def get_mcu(self):
        return self.mcu
    def build_config(self):
        self.mcu.add_config_cmd("config_digital_out oid=%d pin=%s value=%d"
                                " default_value=%d max_duration=%d" % (
                                    self.scl_oid, self.scl_pin, 1, 1, 0))
        self.update_pin_cmd = self.mcu.lookup_command(
            "update_digital_out oid=%c value=%c", cq=self.cmd_queue)
    def i2c_write(self, msg, minclock=0, reqclock=0):
        msg = [self.addr] + msg
        send = self.scl_main.update_pin_cmd.send
        # Send ack
        send([self.sda_oid, 0], minclock=minclock, reqclock=reqclock)
        send([self.scl_oid, 0], minclock=minclock, reqclock=reqclock)
        # Send bytes
        sda_last = 0
        for data in msg:
            # Transmit 8 data bits
            for i in range(8):
                sda_next = not not (data & (0x80 >> i))
                if sda_last != sda_next:
                    sda_last = sda_next
                    send([self.sda_oid, sda_last],
                         minclock=minclock, reqclock=reqclock)
                send([self.scl_oid, 1], minclock=minclock, reqclock=reqclock)
                send([self.scl_oid, 0], minclock=minclock, reqclock=reqclock)
            # Transmit clock for ack
            send([self.scl_oid, 1], minclock=minclock, reqclock=reqclock)
            send([self.scl_oid, 0], minclock=minclock, reqclock=reqclock)
        # Send stop
        if sda_last:
            send([self.sda_oid, 0], minclock=minclock, reqclock=reqclock)
        send([self.scl_oid, 1], minclock=minclock, reqclock=reqclock)
        send([self.sda_oid, 1], minclock=minclock, reqclock=reqclock)

    def i2c_read(self, length, minclock=0, reqclock=0, sda_value=None):
        # Prepare to receive data
        self.update_pin_cmd.send([self.sda_oid, 1], minclock=minclock, reqclock=reqclock)

        # Send start condition and address with read bit set
        self.i2c_write([self.addr | 0x01], minclock=minclock, reqclock=reqclock)

        # Receive bytes
        recv_data = []
        send = self.scl_main.update_pin_cmd.send
        for _ in range(length):
            # Receive 8 data bits
            data = 0
            for i in range(8):
                send([self.scl_oid, 1], minclock=minclock, reqclock=reqclock)
                # Read SDA pin value here and store it in the 'sda_value' variable
                # Use 'sda_value' to update the 'data' variable
                if sda_value:
                    data |= (1 << (7 - i))
                send([self.scl_oid, 0], minclock=minclock, reqclock=reqclock)

            recv_data.append(data)

            # Send ACK/NACK for each byte except the last one
            if len(recv_data) < length:
                send([self.sda_oid, 0], minclock=minclock, reqclock=reqclock)
            send([self.scl_oid, 1], minclock=minclock, reqclock=reqclock)
            send([self.scl_oid, 0], minclock=minclock, reqclock=reqclock)

        # Release SDA line
        send([self.sda_oid, 1], minclock=minclock, reqclock=reqclock)

        # Send stop condition
        send([self.scl_oid, 1], minclock=minclock, reqclock=reqclock)
        send([self.sda_oid, 1], minclock=minclock, reqclock=reqclock)

        return recv_data

class MCU_ADS1100:
    def __init__(self, config):
        self._printer = config.get_printer()
        self._i2c = bus.MCU_I2C_from_config(config, default_addr=ADS1100_CHIP_ADDR, default_speed=ADS1100_I2C_SPEED)
        self._reactor = self._printer.get_reactor()
        self._name = config.get_name().split()[1]
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
        self.setup_adc_callback(0.5, self.adc_callback)


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
    def adc_callback(self, read_time, read_value):
        logging.info('adc_callback')
        logging.info(read_time)
        logging.info(read_value)


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
        config = ADS1100_SAMPLE_RATE_TABLE[self._rate] << 2 | ADS1100_GAIN_TABLE[self._gain]

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
            logging.info(response)

            # retry if response too short
            if len(response) < 2:
                logging.info("ADS1100: conversion failed, trying again...")
                continue

            # return response
            self._conversion_started = False
            return (response, result['#receive_time'])

    def _handle_timer(self, eventtime):
        logging.info('_handle_timer')
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