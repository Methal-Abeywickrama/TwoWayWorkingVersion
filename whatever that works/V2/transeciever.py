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
from gnuradio import blocks, gr
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
from gnuradio import pdu
import pmt
from gnuradio import zeromq
import os
import sip
import threading
import transeciever_epy_block_0 as epy_block_0  # embedded python block
import transeciever_epy_block_0_0 as epy_block_0_0  # embedded python block
import transeciever_epy_block_0_0_0 as epy_block_0_0_0  # embedded python block
import transeciever_epy_block_1 as epy_block_1  # embedded python block
import transeciever_epy_block_2 as epy_block_2  # embedded python block



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
        self.tl_local_addr = tl_local_addr = int(os.environ.get('TL_LOCAL_ADDR', '1'))
        self.BPSK_CONST = BPSK_CONST = digital.constellation_rect([1+0j, -1+0j], [0, 1],
        2, 2, 1, 1, 1).base()
        self.tl_zmq_tx_port = tl_zmq_tx_port = int(os.environ.get('TL_ZMQ_TX_PORT', '52001'))
        self.tl_zmq_rx_port = tl_zmq_rx_port = int(os.environ.get('TL_ZMQ_RX_PORT', '52002'))
        self.tl_rto_ms = tl_rto_ms = int(os.environ.get('TL_RTO_MS', '500'))
        self.tl_role = tl_role = os.environ.get('TL_ROLE', 'initiator')
        self.tl_peer_addr = tl_peer_addr = int(os.environ.get('TL_PEER_ADDR', '2'))
        self.tl_mtu = tl_mtu = int(os.environ.get('TL_MTU', '200'))
        self.tl_max_retries = tl_max_retries = int(os.environ.get('TL_MAX_RETRIES', '10'))
        self.tl_m = tl_m = int(os.environ.get('TL_M', '4'))
        self.tl_local_port = tl_local_port = int(os.environ.get('TL_LOCAL_PORT', '1'))
        self.sym_bw = sym_bw = 0.020
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1.5e6
        self.power_threshold = power_threshold = -25
        self.fll_loop_bw = fll_loop_bw = 0.01
        self.costas_bw = costas_bw = 0.02
        self.alpha = alpha = 0.35
        self.adpt_alg = adpt_alg = digital.adaptive_algorithm_cma( BPSK_CONST, .01, 4).base()
        self.SDR_CF = SDR_CF = 433e6
        self.QPSK_CONST = QPSK_CONST = digital.constellation_rect([-1-1j, -1+1j, 1+1j, 1-1j], [0, 1, 3, 2],
        4, 2, 2, 1, 1).base()
        self.MY_ID = MY_ID = tl_local_addr
        self.CH_GAIN = CH_GAIN = 20.0
        self.ADDR = ADDR = "ip:192.168.1.10"

        ##################################################
        # Blocks
        ##################################################

        self._power_threshold_range = qtgui.Range(-100, 0, 1, -25, 200)
        self._power_threshold_win = qtgui.RangeWidget(self._power_threshold_range, self.set_power_threshold, "'power_threshold'", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._power_threshold_win)
        self.zeromq_push_msg_sink_0 = zeromq.push_msg_sink("tcp://127.0.0.1:" + str(tl_zmq_rx_port), 100, True)
        self.zeromq_pull_msg_source_0 = zeromq.pull_msg_source("tcp://127.0.0.1:" + str(tl_zmq_tx_port), 100, True)
        self.root_raised_cosine_filter_0 = filter.fir_filter_ccf(
            1,
            firdes.root_raised_cosine(
                1,
                samp_rate,
                (samp_rate/float(sps)),
                alpha,
                (11*sps+1)))
        self.qtgui_waterfall_sink_x_0 = qtgui.waterfall_sink_c(
            1024, #size
            window.WIN_BLACKMAN_hARRIS, #wintype
            733e6, #fc
            samp_rate, #bw
            "", #name
            1, #number of inputs
            None # parent
        )
        self.qtgui_waterfall_sink_x_0.set_update_time(0.10)
        self.qtgui_waterfall_sink_x_0.enable_grid(False)
        self.qtgui_waterfall_sink_x_0.enable_axis_labels(True)



        labels = ['', '', '', '', '',
                  '', '', '', '', '']
        colors = [0, 0, 0, 0, 0,
                  0, 0, 0, 0, 0]
        alphas = [1.0, 1.0, 1.0, 1.0, 1.0,
                  1.0, 1.0, 1.0, 1.0, 1.0]

        for i in range(1):
            if len(labels[i]) == 0:
                self.qtgui_waterfall_sink_x_0.set_line_label(i, "Data {0}".format(i))
            else:
                self.qtgui_waterfall_sink_x_0.set_line_label(i, labels[i])
            self.qtgui_waterfall_sink_x_0.set_color_map(i, colors[i])
            self.qtgui_waterfall_sink_x_0.set_line_alpha(i, alphas[i])

        self.qtgui_waterfall_sink_x_0.set_intensity_range(-140, 10)

        self._qtgui_waterfall_sink_x_0_win = sip.wrapinstance(self.qtgui_waterfall_sink_x_0.qwidget(), Qt.QWidget)

        self.top_layout.addWidget(self._qtgui_waterfall_sink_x_0_win)
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
        self.pdu_pdu_filter_0_0 = pdu.pdu_filter(pmt.intern("dst_addr"), pmt.from_uint64(0), False)
        self.pdu_pdu_filter_0 = pdu.pdu_filter(pmt.intern("dst_addr"), pmt.from_uint64(tl_local_addr), False)
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(ADDR if ADDR else iio.get_pluto_uri(), [True, True], 4096)
        self.iio_pluto_source_0.set_len_tag_key('packet_len')
        self.iio_pluto_source_0.set_frequency(int(2.412e9))
        self.iio_pluto_source_0.set_samplerate(int(samp_rate))
        self.iio_pluto_source_0.set_gain_mode(0, 'slow_attack')
        self.iio_pluto_source_0.set_gain(0, 64)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.iio_pluto_sink_0_0 = iio.fmcomms2_sink_fc32(ADDR if ADDR else iio.get_pluto_uri(), [True, True], 33440, False)
        self.iio_pluto_sink_0_0.set_len_tag_key('')
        self.iio_pluto_sink_0_0.set_bandwidth(20000000)
        self.iio_pluto_sink_0_0.set_frequency(int(2.473e9))
        self.iio_pluto_sink_0_0.set_samplerate(int(samp_rate))
        self.iio_pluto_sink_0_0.set_attenuation(0, CH_GAIN)
        self.iio_pluto_sink_0_0.set_filter_params('Auto', '', 0, 0)
        self.epy_block_2 = epy_block_2.blk()
        self.epy_block_1 = epy_block_1.blk(m=tl_m, rto_ms=tl_rto_ms, node_role=tl_role, mtu_bytes=tl_mtu, local_addr=tl_local_addr, local_port=tl_local_port, max_retries=tl_max_retries)
        self.epy_block_0_0_0 = epy_block_0_0_0.PacketDeframerRX(peer_id=tl_local_addr, max_bit_errors=1, max_payload_len=8192, bit_rate=375000)
        self.epy_block_0_0 = epy_block_0_0.PacketFramerTX(peer_id=tl_peer_addr, preamble_len=512, repeat_count=5, max_payload_len=8196, preamble_byte=0xFF, postamble_len=160)
        self.epy_block_0 = epy_block_0.blk()
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
        self.digital_linear_equalizer_0 = digital.linear_equalizer(15, 2, adpt_alg, True, [], "")
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
        self.blocks_message_debug_2 = blocks.message_debug(True, gr.log_levels.info)
        self.blocks_copy_1_2 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_2.set_enabled(True)
        self.blocks_copy_1_1 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_1.set_enabled(True)
        self.blocks_copy_0_0 = blocks.copy(gr.sizeof_char*1)
        self.blocks_copy_0_0.set_enabled(True)
        self.analog_pwr_squelch_xx_0 = analog.pwr_squelch_cc(power_threshold, (1e-4), 0, True)
        self.analog_agc_xx_0 = analog.agc_cc((1e-3), 1.0, 1.0, 1000)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.epy_block_0, 'pdus'), (self.pdu_pdu_filter_0, 'pdus'))
        self.msg_connect((self.epy_block_0, 'pdus'), (self.pdu_pdu_filter_0_0, 'pdus'))
        self.msg_connect((self.epy_block_0_0, 'pdu_out'), (self.pdu_pdu_to_tagged_stream_0_0, 'pdus'))
        self.msg_connect((self.epy_block_0_0_0, 'pdu_out'), (self.epy_block_0, 'pdus'))
        self.msg_connect((self.epy_block_1, 'pdu_out'), (self.epy_block_2, 'pdus'))
        self.msg_connect((self.epy_block_1, 'app_out'), (self.zeromq_push_msg_sink_0, 'in'))
        self.msg_connect((self.epy_block_2, 'pdus'), (self.epy_block_0_0, 'msg_in'))
        self.msg_connect((self.pdu_pdu_filter_0, 'pdus'), (self.epy_block_1, 'pdu_in'))
        self.msg_connect((self.pdu_pdu_filter_0_0, 'pdus'), (self.epy_block_1, 'pdu_in'))
        self.msg_connect((self.zeromq_pull_msg_source_0, 'out'), (self.epy_block_1, 'app_in'))
        self.connect((self.analog_agc_xx_0, 0), (self.analog_pwr_squelch_xx_0, 0))
        self.connect((self.analog_pwr_squelch_xx_0, 0), (self.digital_fll_band_edge_cc_0, 0))
        self.connect((self.blocks_copy_0_0, 0), (self.digital_constellation_modulator_0_0_0, 0))
        self.connect((self.blocks_copy_1_1, 0), (self.analog_agc_xx_0, 0))
        self.connect((self.blocks_copy_1_2, 0), (self.root_raised_cosine_filter_0, 0))
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
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.digital_linear_equalizer_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.blocks_copy_1_1, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.qtgui_waterfall_sink_x_0, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0_0, 0), (self.blocks_copy_0_0, 0))
        self.connect((self.root_raised_cosine_filter_0, 0), (self.digital_symbol_sync_xx_0, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "transeciever")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tl_local_addr(self):
        return self.tl_local_addr

    def set_tl_local_addr(self, tl_local_addr):
        self.tl_local_addr = tl_local_addr
        self.set_MY_ID(self.tl_local_addr)
        self.epy_block_0_0_0.peer_id = self.tl_local_addr
        self.epy_block_1.local_addr = self.tl_local_addr
        self.pdu_pdu_filter_0.set_val(pmt.from_uint64(self.tl_local_addr))

    def get_BPSK_CONST(self):
        return self.BPSK_CONST

    def set_BPSK_CONST(self, BPSK_CONST):
        self.BPSK_CONST = BPSK_CONST
        self.digital_constellation_decoder_cb_0_0.set_constellation(self.BPSK_CONST)

    def get_tl_zmq_tx_port(self):
        return self.tl_zmq_tx_port

    def set_tl_zmq_tx_port(self, tl_zmq_tx_port):
        self.tl_zmq_tx_port = tl_zmq_tx_port

    def get_tl_zmq_rx_port(self):
        return self.tl_zmq_rx_port

    def set_tl_zmq_rx_port(self, tl_zmq_rx_port):
        self.tl_zmq_rx_port = tl_zmq_rx_port

    def get_tl_rto_ms(self):
        return self.tl_rto_ms

    def set_tl_rto_ms(self, tl_rto_ms):
        self.tl_rto_ms = tl_rto_ms

    def get_tl_role(self):
        return self.tl_role

    def set_tl_role(self, tl_role):
        self.tl_role = tl_role

    def get_tl_peer_addr(self):
        return self.tl_peer_addr

    def set_tl_peer_addr(self, tl_peer_addr):
        self.tl_peer_addr = tl_peer_addr
        self.epy_block_0_0.peer_id = self.tl_peer_addr

    def get_tl_mtu(self):
        return self.tl_mtu

    def set_tl_mtu(self, tl_mtu):
        self.tl_mtu = tl_mtu

    def get_tl_max_retries(self):
        return self.tl_max_retries

    def set_tl_max_retries(self, tl_max_retries):
        self.tl_max_retries = tl_max_retries
        self.epy_block_1.max_retries = self.tl_max_retries

    def get_tl_m(self):
        return self.tl_m

    def set_tl_m(self, tl_m):
        self.tl_m = tl_m
        self.epy_block_1.m = self.tl_m

    def get_tl_local_port(self):
        return self.tl_local_port

    def set_tl_local_port(self, tl_local_port):
        self.tl_local_port = tl_local_port
        self.epy_block_1.local_port = self.tl_local_port

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
        self.iio_pluto_sink_0_0.set_samplerate(int(self.samp_rate))
        self.iio_pluto_source_0.set_samplerate(int(self.samp_rate))
        self.qtgui_freq_sink_x_1.set_frequency_range(0, self.samp_rate)
        self.qtgui_waterfall_sink_x_0.set_frequency_range(733e6, self.samp_rate)
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_power_threshold(self):
        return self.power_threshold

    def set_power_threshold(self, power_threshold):
        self.power_threshold = power_threshold
        self.analog_pwr_squelch_xx_0.set_threshold(self.power_threshold)

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

    def get_alpha(self):
        return self.alpha

    def set_alpha(self, alpha):
        self.alpha = alpha
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_adpt_alg(self):
        return self.adpt_alg

    def set_adpt_alg(self, adpt_alg):
        self.adpt_alg = adpt_alg

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

    def get_CH_GAIN(self):
        return self.CH_GAIN

    def set_CH_GAIN(self, CH_GAIN):
        self.CH_GAIN = CH_GAIN
        self.iio_pluto_sink_0_0.set_attenuation(0,self.CH_GAIN)

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
