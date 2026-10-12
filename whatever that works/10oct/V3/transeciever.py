#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: CDP transeciever
# Author: mushab404
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from gnuradio import analog
from gnuradio import blocks
from gnuradio import digital
from gnuradio import filter
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import gr, pdu
from gnuradio import iio
import numpy as np
import sip
import threading
import time
import transeciever_epy_block_0_0 as epy_block_0_0  # embedded python block
import transeciever_epy_block_0_0_0 as epy_block_0_0_0  # embedded python block
import transeciever_epy_block_corr as epy_block_corr  # embedded python block
import transeciever_epy_noise as epy_noise  # embedded python block



class transeciever(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "CDP transeciever ", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("CDP transeciever ")
        qtgui.util.check_set_qss()
        try:
            self.setWindowIcon(Qt.QIcon.fromTheme('gnuradio-grc'))
        except BaseException as exc:
            print(f"Qt GUI: Could not set Icon: {str(exc)}", file=sys.stderr)
        self.top_scroll_layout = Qt.QVBoxLayout()
        self.setLayout(self.top_scroll_layout)
        self.top_scroll = Qt.QScrollArea()
        self.top_scroll.setFrameStyle(Qt.QFrame.NoFrame)
        self.top_scroll_layout.addWidget(self.top_scroll)
        self.top_scroll.setWidgetResizable(True)
        self.top_widget = Qt.QWidget()
        self.top_scroll.setWidget(self.top_widget)
        self.top_layout = Qt.QVBoxLayout(self.top_widget)
        self.top_grid_layout = Qt.QGridLayout()
        self.top_layout.addLayout(self.top_grid_layout)

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "transeciever")

        try:
            geometry = self.settings.value("geometry")
            if geometry:
                self.restoreGeometry(geometry)
        except BaseException as exc:
            print(f"Qt GUI: Could not restore geometry: {str(exc)}", file=sys.stderr)
        self.flowgraph_started = threading.Event()

        ##################################################
        # Variables
        ##################################################
        self.alpha = alpha = 0.35
        self.TRAIN_BITS = TRAIN_BITS = np.unpackbits(np.frombuffer(bytes([0xF8, 0xDD, 0x42, 0x58]), np.uint8))
        self.RC_X = RC_X = np.arange(-8, 9)/2.0
        self.TRAIN_SYMS = TRAIN_SYMS = list(map(complex, 1 - 2*np.bitwise_xor.accumulate(TRAIN_BITS).astype(int)))
        self.RX_AMP = RX_AMP = 1.0
        self.RC_PULSE = RC_PULSE = np.sinc(RC_X)*np.cos(np.pi*alpha*RC_X)/(1-(2*alpha*RC_X)**2)
        self.BPSK_CONST = BPSK_CONST = digital.constellation_rect([1+0j, -1+0j], [0, 1],
        2, 2, 1, 1, 1).base()
        self.train_delay = train_delay = 0
        self.sym_bw = sym_bw = 0.020
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1.5e6
        self.power_threshold = power_threshold = -40
        self.max_payload_len = max_payload_len = 2048
        self.margin_db = margin_db = 6
        self.fll_loop_bw = fll_loop_bw = 0.01
        self.costas_bw = costas_bw = 0.05
        self.corr_thr = corr_thr = 0.7
        self.adpt_alg = adpt_alg = digital.adaptive_algorithm_nlms( BPSK_CONST, 0.5).base()
        self.TRAIN_REF = TRAIN_REF = list(map(complex, RX_AMP*np.convolve(np.kron(TRAIN_SYMS, [1, 0]), RC_PULSE)[8:8+2*len(TRAIN_SYMS)-1]))
        self.SDR_CF = SDR_CF = 433e6
        self.QPSK_CONST = QPSK_CONST = digital.constellation_rect([-1-1j, -1+1j, 1+1j, 1-1j], [0, 1, 3, 2],
        4, 2, 2, 1, 1).base()
        self.MY_ID = MY_ID = 0
        self.CH_GAIN = CH_GAIN = 20.0
        self.BUFFER_SIZE = BUFFER_SIZE = 4096
        self.ADDR = ADDR = "ip:192.168.1.10"

        ##################################################
        # Blocks
        ##################################################

        self._margin_db_range = qtgui.Range(2, 20, 0.5, 6, 200)
        self._margin_db_win = qtgui.RangeWidget(self._margin_db_range, self.set_margin_db, "Squelch margin over noise (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._margin_db_win)
        self.epy_noise = epy_noise.NoiseFloor(samp_rate=samp_rate, margin_db=margin_db, window_s=1.0, percentile=10.0, rise_db_s=1.0, thr_min=-100.0, thr_max=-20.0, init_db=-40.0, verbose=1)
        self._train_delay_range = qtgui.Range(0, 12, 1, 0, 200)
        self._train_delay_win = qtgui.RangeWidget(self._train_delay_range, self.set_train_delay, "'train_delay'", "counter_slider", int, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._train_delay_win)
        def _power_threshold_probe():
          self.flowgraph_started.wait()
          while True:

            val = self.epy_noise.level()
            try:
              try:
                self.doc.add_next_tick_callback(functools.partial(self.set_power_threshold,val))
              except AttributeError:
                self.set_power_threshold(val)
            except AttributeError:
              pass
            time.sleep(1.0 / (5))
        _power_threshold_thread = threading.Thread(target=_power_threshold_probe)
        _power_threshold_thread.daemon = True
        _power_threshold_thread.start()
        self._corr_thr_range = qtgui.Range(0.3, 0.98, 0.01, 0.7, 200)
        self._corr_thr_win = qtgui.RangeWidget(self._corr_thr_range, self.set_corr_thr, "'corr_thr'", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._corr_thr_win)
        self.single_pole_iir_filter_xx_0 = filter.single_pole_iir_filter_ff(0.01, 1)
        self.root_raised_cosine_filter_0 = filter.fir_filter_ccf(
            1,
            firdes.root_raised_cosine(
                1,
                samp_rate,
                (samp_rate/float(sps)),
                alpha,
                (11*sps+1)))
        self.qtgui_time_sink_x_0 = qtgui.time_sink_f(
            1024, #size
            samp_rate, #samp_rate
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_time_sink_x_0.set_update_time(0.10)
        self.qtgui_time_sink_x_0.set_y_axis(-55, 0)

        self.qtgui_time_sink_x_0.set_y_label('Amplitude', "")

        self.qtgui_time_sink_x_0.enable_tags(True)
        self.qtgui_time_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, 0, "")
        self.qtgui_time_sink_x_0.enable_autoscale(False)
        self.qtgui_time_sink_x_0.enable_grid(False)
        self.qtgui_time_sink_x_0.enable_axis_labels(True)
        self.qtgui_time_sink_x_0.enable_control_panel(False)
        self.qtgui_time_sink_x_0.enable_stem_plot(False)


        labels = ['Signal 1', 'Signal 2', 'Signal 3', 'Signal 4', 'Signal 5',
            'Signal 6', 'Signal 7', 'Signal 8', 'Signal 9', 'Signal 10']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ['blue', 'red', 'green', 'black', 'cyan',
            'magenta', 'yellow', 'dark red', 'dark green', 'dark blue']
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]
        styles = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        markers = [-1, -1, -1, -1, -1,
            -1, -1, -1, -1, -1]


        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_time_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_time_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_time_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_time_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_time_sink_x_0.set_line_style(i, styles[i])
            self.qtgui_time_sink_x_0.set_line_marker(i, markers[i])
            self.qtgui_time_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_time_sink_x_0_win = sip.wrapinstance(self.qtgui_time_sink_x_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_time_sink_x_0_win)
        self.qtgui_freq_sink_x_1 = qtgui.freq_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            0, #fc
            samp_rate, #bw
            "", #name
            1,
            None # parent
        )
        self.qtgui_freq_sink_x_1.set_update_time(0.10)
        self.qtgui_freq_sink_x_1.set_y_axis((-140), 10)
        self.qtgui_freq_sink_x_1.set_y_label('Relative Gain', 'dB')
        self.qtgui_freq_sink_x_1.set_trigger_mode(qtgui.TRIG_MODE_FREE, 0.0, 0, "")
        self.qtgui_freq_sink_x_1.enable_autoscale(False)
        self.qtgui_freq_sink_x_1.enable_grid(False)
        self.qtgui_freq_sink_x_1.set_fft_average(1.0)
        self.qtgui_freq_sink_x_1.enable_axis_labels(True)
        self.qtgui_freq_sink_x_1.enable_control_panel(False)
        self.qtgui_freq_sink_x_1.set_fft_window_normalized(False)



        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_freq_sink_x_1.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_freq_sink_x_1.set_line_label(i, labels[i])
            self.qtgui_freq_sink_x_1.set_line_width(i, widths[i])
            self.qtgui_freq_sink_x_1.set_line_color(i, colors[i])
            self.qtgui_freq_sink_x_1.set_line_alpha(i, alphas[i])

        self._qtgui_freq_sink_x_1_win = sip.wrapinstance(self.qtgui_freq_sink_x_1.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_freq_sink_x_1_win)
        self.qtgui_edit_box_msg_0_0 = qtgui.edit_box_msg(qtgui.STRING, "MESSAGE", "TX Message", False, False, '', None)
        self._qtgui_edit_box_msg_0_0_win = sip.wrapinstance(self.qtgui_edit_box_msg_0_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_edit_box_msg_0_0_win)
        self.qtgui_const_sink_x_1_0 = qtgui.const_sink_c(
            1024, #size
            "before costas", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_const_sink_x_1_0.set_update_time(0.10)
        self.qtgui_const_sink_x_1_0.set_y_axis((-2), 2)
        self.qtgui_const_sink_x_1_0.set_x_axis((-2), 2)
        self.qtgui_const_sink_x_1_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.qtgui_const_sink_x_1_0.enable_autoscale(False)
        self.qtgui_const_sink_x_1_0.enable_grid(False)
        self.qtgui_const_sink_x_1_0.enable_axis_labels(True)


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_const_sink_x_1_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_const_sink_x_1_0.set_line_label(i, labels[i])
            self.qtgui_const_sink_x_1_0.set_line_width(i, widths[i])
            self.qtgui_const_sink_x_1_0.set_line_color(i, colors[i])
            self.qtgui_const_sink_x_1_0.set_line_style(i, styles[i])
            self.qtgui_const_sink_x_1_0.set_line_marker(i, markers[i])
            self.qtgui_const_sink_x_1_0.set_line_alpha(i, alphas[i])

        self._qtgui_const_sink_x_1_0_win = sip.wrapinstance(self.qtgui_const_sink_x_1_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_const_sink_x_1_0_win)
        self.qtgui_const_sink_x_1 = qtgui.const_sink_c(
            1024, #size
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_const_sink_x_1.set_update_time(0.10)
        self.qtgui_const_sink_x_1.set_y_axis((-2), 2)
        self.qtgui_const_sink_x_1.set_x_axis((-2), 2)
        self.qtgui_const_sink_x_1.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.qtgui_const_sink_x_1.enable_autoscale(False)
        self.qtgui_const_sink_x_1.enable_grid(False)
        self.qtgui_const_sink_x_1.enable_axis_labels(True)


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_const_sink_x_1.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_const_sink_x_1.set_line_label(i, labels[i])
            self.qtgui_const_sink_x_1.set_line_width(i, widths[i])
            self.qtgui_const_sink_x_1.set_line_color(i, colors[i])
            self.qtgui_const_sink_x_1.set_line_style(i, styles[i])
            self.qtgui_const_sink_x_1.set_line_marker(i, markers[i])
            self.qtgui_const_sink_x_1.set_line_alpha(i, alphas[i])

        self._qtgui_const_sink_x_1_win = sip.wrapinstance(self.qtgui_const_sink_x_1.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_const_sink_x_1_win)
        self.qtgui_const_sink_x_0 = qtgui.const_sink_c(
            1024, #size
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_const_sink_x_0.set_update_time(0.10)
        self.qtgui_const_sink_x_0.set_y_axis((-2), 2)
        self.qtgui_const_sink_x_0.set_x_axis((-2), 2)
        self.qtgui_const_sink_x_0.set_trigger_mode(qtgui.TRIG_MODE_FREE, qtgui.TRIG_SLOPE_POS, 0.0, 0, "")
        self.qtgui_const_sink_x_0.enable_autoscale(False)
        self.qtgui_const_sink_x_0.enable_grid(False)
        self.qtgui_const_sink_x_0.enable_axis_labels(True)


        labels = ['', '', '', '', '',
            '', '', '', '', '']
        widths = [1, 1, 1, 1, 1,
            1, 1, 1, 1, 1]
        colors = ["blue", "red", "green", "black", "cyan",
            "magenta", "yellow", "dark red", "dark green", "dark blue"]
        styles = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        markers = [0, 0, 0, 0, 0,
            0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
            1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_const_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_const_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_const_sink_x_0.set_line_width(i, widths[i])
            self.qtgui_const_sink_x_0.set_line_color(i, colors[i])
            self.qtgui_const_sink_x_0.set_line_style(i, styles[i])
            self.qtgui_const_sink_x_0.set_line_marker(i, markers[i])
            self.qtgui_const_sink_x_0.set_line_alpha(i, alphas[i])

        self._qtgui_const_sink_x_0_win = sip.wrapinstance(self.qtgui_const_sink_x_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_const_sink_x_0_win)
        self.pdu_pdu_to_tagged_stream_0_0 = pdu.pdu_to_tagged_stream(gr.types.byte_t, 'packet_len')
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(ADDR if ADDR else iio.get_pluto_uri(), [True, True], BUFFER_SIZE)
        self.iio_pluto_source_0.set_len_tag_key('packet_len')
        self.iio_pluto_source_0.set_frequency(int(1.27e9))
        self.iio_pluto_source_0.set_samplerate(int(samp_rate))
        self.iio_pluto_source_0.set_gain_mode(0, 'manual')
        self.iio_pluto_source_0.set_gain(0, 64)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.iio_pluto_sink_0_0 = iio.fmcomms2_sink_fc32(ADDR if ADDR else iio.get_pluto_uri(), [True, True], BUFFER_SIZE, False)
        self.iio_pluto_sink_0_0.set_len_tag_key('')
        self.iio_pluto_sink_0_0.set_bandwidth(1500000)
        self.iio_pluto_sink_0_0.set_frequency(int(1.25e9))
        self.iio_pluto_sink_0_0.set_samplerate(int(samp_rate))
        self.iio_pluto_sink_0_0.set_attenuation(0, CH_GAIN)
        self.iio_pluto_sink_0_0.set_filter_params('Auto', '', 0, 0)
        self.epy_block_corr = epy_block_corr.TrainCorr(ref=TRAIN_REF, thr=corr_thr, mark_delay=train_delay, verbose=1)
        self.epy_block_0_0_0 = epy_block_0_0_0.PacketDeframerRX(peer_id=MY_ID, max_bit_errors=1, max_payload_len=max_payload_len, bit_rate=375000)
        self.epy_block_0_0 = epy_block_0_0.PacketFramerTX(peer_id=1, preamble_len=128, repeat_count=1, max_payload_len=max_payload_len, preamble_byte=0xFF, buffer_size=BUFFER_SIZE, postamble_byte=0xFF, train_enable=1)
        self.digital_symbol_sync_xx_0 = digital.symbol_sync_cc(
            digital.TED_SIGNAL_TIMES_SLOPE_ML,
            sps,
            sym_bw,
            1.0,
            1.0,
            0.05,
            2,
            digital.constellation_bpsk().base(),
            digital.IR_MMSE_8TAP,
            128,
            [])
        self.digital_linear_equalizer_0 = digital.linear_equalizer(11, 2, adpt_alg, True, TRAIN_SYMS, "corr_est")
        self.digital_fll_band_edge_cc_0 = digital.fll_band_edge_cc(sps, alpha, (11* sps +1), fll_loop_bw)
        self.digital_diff_decoder_bb_0_0 = digital.diff_decoder_bb(2, digital.DIFF_DIFFERENTIAL)
        self.digital_costas_loop_cc_0 = digital.costas_loop_cc(costas_bw, BPSK_CONST.arity(), False)
        self.digital_constellation_modulator_0_0_0 = digital.generic_mod(
            constellation=BPSK_CONST,
            differential=True,
            samples_per_symbol=sps,
            pre_diff_code=True,
            excess_bw=alpha,
            verbose=False,
            log=False,
            truncate=False)
        self.digital_constellation_decoder_cb_0_0 = digital.constellation_decoder_cb(BPSK_CONST)
        self.blocks_unpack_k_bits_bb_0_0 = blocks.unpack_k_bits_bb(1)
        self.blocks_tag_gate_0_0 = blocks.tag_gate(gr.sizeof_gr_complex * 1, False)
        self.blocks_tag_gate_0_0.set_single_key("")
        self.blocks_tag_debug_corr = blocks.tag_debug(gr.sizeof_gr_complex*1, 'corr_est', "corr_est")
        self.blocks_tag_debug_corr.set_display(True)
        self.blocks_tag_debug_0 = blocks.tag_debug(gr.sizeof_char*1, '', "")
        self.blocks_tag_debug_0.set_display(True)
        self.blocks_nlog10_ff_0 = blocks.nlog10_ff(10, 1, 0)
        self.blocks_copy_1_2 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_2.set_enabled(True)
        self.blocks_copy_1_0_0 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_0_0.set_enabled(True)
        self.blocks_copy_0_0 = blocks.copy(gr.sizeof_char*1)
        self.blocks_copy_0_0.set_enabled(True)
        self.blocks_complex_to_mag_squared_0 = blocks.complex_to_mag_squared(1)
        self.analog_pwr_squelch_xx_0_0 = analog.pwr_squelch_cc(power_threshold, (1e-3), 0, True)
        self.analog_agc_xx_0 = analog.agc_cc((8e-3), 1.0, 1.0, 1000)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.epy_block_0_0, 'pdu_out'), (self.pdu_pdu_to_tagged_stream_0_0, 'pdus'))
        self.msg_connect((self.qtgui_edit_box_msg_0_0, 'msg'), (self.epy_block_0_0, 'msg_in'))
        self.connect((self.analog_agc_xx_0, 0), (self.blocks_copy_1_0_0, 0))
        self.connect((self.analog_pwr_squelch_xx_0_0, 0), (self.analog_agc_xx_0, 0))
        self.connect((self.blocks_complex_to_mag_squared_0, 0), (self.single_pole_iir_filter_xx_0, 0))
        self.connect((self.blocks_copy_0_0, 0), (self.digital_constellation_modulator_0_0_0, 0))
        self.connect((self.blocks_copy_1_0_0, 0), (self.digital_fll_band_edge_cc_0, 0))
        self.connect((self.blocks_copy_1_2, 0), (self.root_raised_cosine_filter_0, 0))
        self.connect((self.blocks_nlog10_ff_0, 0), (self.qtgui_time_sink_x_0, 0))
        self.connect((self.blocks_tag_gate_0_0, 0), (self.iio_pluto_sink_0_0, 0))
        self.connect((self.blocks_unpack_k_bits_bb_0_0, 0), (self.epy_block_0_0_0, 0))
        self.connect((self.digital_constellation_decoder_cb_0_0, 0), (self.digital_diff_decoder_bb_0_0, 0))
        self.connect((self.digital_constellation_modulator_0_0_0, 0), (self.blocks_tag_gate_0_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.digital_constellation_decoder_cb_0_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_const_sink_x_1, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_freq_sink_x_1, 0))
        self.connect((self.digital_diff_decoder_bb_0_0, 0), (self.blocks_unpack_k_bits_bb_0_0, 0))
        self.connect((self.digital_fll_band_edge_cc_0, 0), (self.blocks_copy_1_2, 0))
        self.connect((self.digital_linear_equalizer_0, 0), (self.digital_costas_loop_cc_0, 0))
        self.connect((self.digital_linear_equalizer_0, 0), (self.qtgui_const_sink_x_1_0, 0))
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.epy_block_corr, 0))
        self.connect((self.epy_block_corr, 0), (self.blocks_tag_debug_corr, 0))
        self.connect((self.epy_block_corr, 0), (self.digital_linear_equalizer_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.analog_pwr_squelch_xx_0_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.blocks_complex_to_mag_squared_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.epy_noise, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0_0, 0), (self.blocks_copy_0_0, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0_0, 0), (self.blocks_tag_debug_0, 0))
        self.connect((self.root_raised_cosine_filter_0, 0), (self.digital_symbol_sync_xx_0, 0))
        self.connect((self.single_pole_iir_filter_xx_0, 0), (self.blocks_nlog10_ff_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "transeciever")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_alpha(self):
        return self.alpha

    def set_alpha(self, alpha):
        self.alpha = alpha
        self.set_RC_PULSE(np.sinc(self.RC_X)*np.cos(np.pi*self.alpha*self.RC_X)/(1-(2*self.alpha*self.RC_X)**2))
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_TRAIN_BITS(self):
        return self.TRAIN_BITS

    def set_TRAIN_BITS(self, TRAIN_BITS):
        self.TRAIN_BITS = TRAIN_BITS
        self.set_TRAIN_SYMS(list(map(complex, 1 - 2*np.bitwise_xor.accumulate(self.TRAIN_BITS).astype(int))))

    def get_RC_X(self):
        return self.RC_X

    def set_RC_X(self, RC_X):
        self.RC_X = RC_X
        self.set_RC_PULSE(np.sinc(self.RC_X)*np.cos(np.pi*self.alpha*self.RC_X)/(1-(2*self.alpha*self.RC_X)**2))

    def get_TRAIN_SYMS(self):
        return self.TRAIN_SYMS

    def set_TRAIN_SYMS(self, TRAIN_SYMS):
        self.TRAIN_SYMS = TRAIN_SYMS
        self.set_TRAIN_REF(list(map(complex, self.RX_AMP*np.convolve(np.kron(self.TRAIN_SYMS, [1, 0]), self.RC_PULSE)[8:8+2*len(self.TRAIN_SYMS)-1])))

    def get_RX_AMP(self):
        return self.RX_AMP

    def set_RX_AMP(self, RX_AMP):
        self.RX_AMP = RX_AMP
        self.set_TRAIN_REF(list(map(complex, self.RX_AMP*np.convolve(np.kron(self.TRAIN_SYMS, [1, 0]), self.RC_PULSE)[8:8+2*len(self.TRAIN_SYMS)-1])))

    def get_RC_PULSE(self):
        return self.RC_PULSE

    def set_RC_PULSE(self, RC_PULSE):
        self.RC_PULSE = RC_PULSE
        self.set_TRAIN_REF(list(map(complex, self.RX_AMP*np.convolve(np.kron(self.TRAIN_SYMS, [1, 0]), self.RC_PULSE)[8:8+2*len(self.TRAIN_SYMS)-1])))

    def get_BPSK_CONST(self):
        return self.BPSK_CONST

    def set_BPSK_CONST(self, BPSK_CONST):
        self.BPSK_CONST = BPSK_CONST
        self.digital_constellation_decoder_cb_0_0.set_constellation(self.BPSK_CONST)

    def get_train_delay(self):
        return self.train_delay

    def set_train_delay(self, train_delay):
        self.train_delay = train_delay
        self.epy_block_corr.mark_delay = self.train_delay

    def get_sym_bw(self):
        return self.sym_bw

    def set_sym_bw(self, sym_bw):
        self.sym_bw = sym_bw
        self.digital_symbol_sync_xx_0.set_loop_bandwidth(self.sym_bw)

    def get_sps(self):
        return self.sps

    def set_sps(self, sps):
        self.sps = sps
        self.digital_symbol_sync_xx_0.set_sps(self.sps)
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.epy_noise.samp_rate = self.samp_rate
        self.iio_pluto_sink_0_0.set_samplerate(int(self.samp_rate))
        self.iio_pluto_source_0.set_samplerate(int(self.samp_rate))
        self.qtgui_freq_sink_x_1.set_frequency_range(0, self.samp_rate)
        self.qtgui_time_sink_x_0.set_samp_rate(self.samp_rate)
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_power_threshold(self):
        return self.power_threshold

    def set_power_threshold(self, power_threshold):
        self.power_threshold = power_threshold
        self.analog_pwr_squelch_xx_0_0.set_threshold(self.power_threshold)

    def get_max_payload_len(self):
        return self.max_payload_len

    def set_max_payload_len(self, max_payload_len):
        self.max_payload_len = max_payload_len
        self.epy_block_0_0.max_payload_len = self.max_payload_len
        self.epy_block_0_0_0.max_payload_len = self.max_payload_len

    def get_margin_db(self):
        return self.margin_db

    def set_margin_db(self, margin_db):
        self.margin_db = margin_db
        self.epy_noise.margin_db = self.margin_db

    def get_fll_loop_bw(self):
        return self.fll_loop_bw

    def set_fll_loop_bw(self, fll_loop_bw):
        self.fll_loop_bw = fll_loop_bw
        self.digital_fll_band_edge_cc_0.set_loop_bandwidth(self.fll_loop_bw)

    def get_costas_bw(self):
        return self.costas_bw

    def set_costas_bw(self, costas_bw):
        self.costas_bw = costas_bw
        self.digital_costas_loop_cc_0.set_loop_bandwidth(self.costas_bw)

    def get_corr_thr(self):
        return self.corr_thr

    def set_corr_thr(self, corr_thr):
        self.corr_thr = corr_thr
        self.epy_block_corr.thr = self.corr_thr

    def get_adpt_alg(self):
        return self.adpt_alg

    def set_adpt_alg(self, adpt_alg):
        self.adpt_alg = adpt_alg

    def get_TRAIN_REF(self):
        return self.TRAIN_REF

    def set_TRAIN_REF(self, TRAIN_REF):
        self.TRAIN_REF = TRAIN_REF

    def get_SDR_CF(self):
        return self.SDR_CF

    def set_SDR_CF(self, SDR_CF):
        self.SDR_CF = SDR_CF

    def get_QPSK_CONST(self):
        return self.QPSK_CONST

    def set_QPSK_CONST(self, QPSK_CONST):
        self.QPSK_CONST = QPSK_CONST

    def get_MY_ID(self):
        return self.MY_ID

    def set_MY_ID(self, MY_ID):
        self.MY_ID = MY_ID
        self.epy_block_0_0_0.peer_id = self.MY_ID

    def get_CH_GAIN(self):
        return self.CH_GAIN

    def set_CH_GAIN(self, CH_GAIN):
        self.CH_GAIN = CH_GAIN
        self.iio_pluto_sink_0_0.set_attenuation(0,self.CH_GAIN)

    def get_BUFFER_SIZE(self):
        return self.BUFFER_SIZE

    def set_BUFFER_SIZE(self, BUFFER_SIZE):
        self.BUFFER_SIZE = BUFFER_SIZE
        self.epy_block_0_0.buffer_size = self.BUFFER_SIZE

    def get_ADDR(self):
        return self.ADDR

    def set_ADDR(self, ADDR):
        self.ADDR = ADDR




def main(top_block_cls=transeciever, options=None):

    qapp = Qt.QApplication(sys.argv)

    tb = top_block_cls()

    tb.start()
    tb.flowgraph_started.set()

    tb.show()

    def sig_handler(sig=None, frame=None):
        tb.stop()
        tb.wait()

        Qt.QApplication.quit()

    signal.signal(signal.SIGINT, sig_handler)
    signal.signal(signal.SIGTERM, sig_handler)

    timer = Qt.QTimer()
    timer.start(500)
    timer.timeout.connect(lambda: None)

    qapp.exec_()

if __name__ == '__main__':
    main()
