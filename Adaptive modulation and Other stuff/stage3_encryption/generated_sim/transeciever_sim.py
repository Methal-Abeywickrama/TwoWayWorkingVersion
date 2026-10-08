#!/usr/bin/env python3
# -*- coding: utf-8 -*-

#
# SPDX-License-Identifier: GPL-3.0
#
# GNU Radio Python Flow Graph
# Title: CDP transceiver - Stage 3 (adaptive + encryption) (SIM: ZMQ IQ, no radio)
# Author: mushab404
# GNU Radio version: 3.10.12.0

from PyQt5 import Qt
from gnuradio import qtgui
from PyQt5 import QtCore
from PyQt5.QtCore import QObject, pyqtSlot
from gnuradio import analog
from gnuradio import blocks
from gnuradio import blocks, gr
from gnuradio import channels
from gnuradio.filter import firdes
from gnuradio import digital
from gnuradio import filter
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import eng_notation
from gnuradio import gr, pdu
from gnuradio import pdu
import pmt
from gnuradio import zeromq
import os
import sip
import threading
import transeciever_sim_epy_block_0 as epy_block_0  # embedded python block
import transeciever_sim_epy_block_0_0 as epy_block_0_0  # embedded python block
import transeciever_sim_epy_block_0_0_0 as epy_block_0_0_0  # embedded python block
import transeciever_sim_epy_block_1 as epy_block_1  # embedded python block
import transeciever_sim_epy_block_2 as epy_block_2  # embedded python block
import transeciever_sim_epy_block_3 as epy_block_3  # embedded python block
import transeciever_sim_epy_block_4 as epy_block_4  # embedded python block
import transeciever_sim_epy_block_5 as epy_block_5  # embedded python block



class transeciever_sim(gr.top_block, Qt.QWidget):

    def __init__(self):
        gr.top_block.__init__(self, "CDP transceiver - Stage 3 (adaptive + encryption) (SIM: ZMQ IQ, no radio)", catch_exceptions=True)
        Qt.QWidget.__init__(self)
        self.setWindowTitle("CDP transceiver - Stage 3 (adaptive + encryption) (SIM: ZMQ IQ, no radio)")
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

        self.settings = Qt.QSettings("gnuradio/flowgraphs", "transeciever_sim")

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
        self.tl_peer_addr = tl_peer_addr = int(os.environ.get('TL_PEER_ADDR', '2'))
        self.tl_local_addr = tl_local_addr = int(os.environ.get('TL_LOCAL_ADDR', '1'))
        self.fdd_side = fdd_side = os.environ.get('TL_FDD_SIDE', 'A' if tl_local_addr < tl_peer_addr else 'B')
        self.TX_FREQ_B = TX_FREQ_B = 2437e6
        self.TX_FREQ_A = TX_FREQ_A = 2412e6
        self.BPSK_CONST = BPSK_CONST = digital.constellation_rect([1+0j, -1+0j], [0, 1],
        2, 2, 1, 1, 1).base()
        self.tx_freq = tx_freq = TX_FREQ_A if fdd_side == 'A' else TX_FREQ_B
        self.tl_zmq_tx_port = tl_zmq_tx_port = int(os.environ.get('TL_ZMQ_TX_PORT', '52001'))
        self.tl_zmq_rx_port = tl_zmq_rx_port = int(os.environ.get('TL_ZMQ_RX_PORT', '52002'))
        self.tl_rto_ms = tl_rto_ms = int(os.environ.get('TL_RTO_MS', '500'))
        self.tl_role = tl_role = os.environ.get('TL_ROLE', 'initiator')
        self.tl_mtu = tl_mtu = int(os.environ.get('TL_MTU', '200'))
        self.tl_max_retries = tl_max_retries = int(os.environ.get('TL_MAX_RETRIES', '10'))
        self.tl_m = tl_m = int(os.environ.get('TL_M', '4'))
        self.tl_local_port = tl_local_port = int(os.environ.get('TL_LOCAL_PORT', '1'))
        self.sym_bw = sym_bw = 0.020
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1.5e6
        self.rx_freq = rx_freq = TX_FREQ_B if fdd_side == 'A' else TX_FREQ_A
        self.nv = nv = float(os.environ.get('TL_NOISE', '0'))
        self.iq_tx_port = iq_tx_port = 53001 if fdd_side == 'A' else 53002
        self.iq_rx_port = iq_rx_port = 53002 if fdd_side == 'A' else 53001
        self.fll_loop_bw = fll_loop_bw = 0.01
        self.costas_bw = costas_bw = 0.02
        self.alpha = alpha = 0.35
        self.adpt_alg = adpt_alg = digital.adaptive_algorithm_cma( BPSK_CONST, .01, 4).base()
        self.SDR_CF = SDR_CF = 433e6
        self.QPSK_CONST = QPSK_CONST = digital.constellation_rect([-1-1j, -1+1j, 1+1j, 1-1j], [0, 1, 3, 2],
        4, 2, 2, 1, 1).base()
        self.PUBLIC_KEY_PEER = PUBLIC_KEY_PEER = int(os.environ.get('TL_PEER_PUBKEY', '17'))
        self.PUBLIC_KEY_MINE = PUBLIC_KEY_MINE = int(os.environ.get('TL_PUBKEY', '17'))
        self.PRIVATE_KEY = PRIVATE_KEY = int(os.environ.get('TL_PRIVKEY', '241'))
        self.PRIME = PRIME = 257
        self.MY_ID = MY_ID = tl_local_addr
        self.MCS_MODE = MCS_MODE = (int(os.environ.get('TL_MCS_MODE', '-1')))
        self.CH_GAIN = CH_GAIN = 20.0
        self.ADDR = ADDR = os.environ.get('TL_SDR_URI', 'ip:192.168.1.10')

        ##################################################
        # Blocks
        ##################################################

        self._nv_range = qtgui.Range(0, 1, 0.01, float(os.environ.get('TL_NOISE', '0')), 200)
        self._nv_win = qtgui.RangeWidget(self._nv_range, self.set_nv, "RX extra noise (nv)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._nv_win)
        # Create the options list
        self._MCS_MODE_options = [-1, 0, 1]
        # Create the labels list
        self._MCS_MODE_labels = ['Auto', 'BPSK', 'QPSK']
        # Create the combo box
        # Create the radio buttons
        self._MCS_MODE_group_box = Qt.QGroupBox("Modulation" + ": ")
        self._MCS_MODE_box = Qt.QHBoxLayout()
        class variable_chooser_button_group(Qt.QButtonGroup):
            def __init__(self, parent=None):
                Qt.QButtonGroup.__init__(self, parent)
            @pyqtSlot(int)
            def updateButtonChecked(self, button_id):
                self.button(button_id).setChecked(True)
        self._MCS_MODE_button_group = variable_chooser_button_group()
        self._MCS_MODE_group_box.setLayout(self._MCS_MODE_box)
        for i, _label in enumerate(self._MCS_MODE_labels):
            radio_button = Qt.QRadioButton(_label)
            self._MCS_MODE_box.addWidget(radio_button)
            self._MCS_MODE_button_group.addButton(radio_button, i)
        self._MCS_MODE_callback = lambda i: Qt.QMetaObject.invokeMethod(self._MCS_MODE_button_group, "updateButtonChecked", Qt.Q_ARG("int", self._MCS_MODE_options.index(i)))
        self._MCS_MODE_callback(self.MCS_MODE)
        self._MCS_MODE_button_group.buttonClicked[int].connect(
            lambda i: self.set_MCS_MODE(self._MCS_MODE_options[i]))
        self.top_layout.addWidget(self._MCS_MODE_group_box)
        self.zeromq_sub_source_iq = zeromq.sub_source(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:" + str(iq_rx_port), 100, False, (-1), '', False)
        self.zeromq_push_msg_sink_0 = zeromq.push_msg_sink("tcp://127.0.0.1:" + str(tl_zmq_rx_port), 100, True)
        self.zeromq_pull_msg_source_0 = zeromq.pull_msg_source("tcp://127.0.0.1:" + str(tl_zmq_tx_port), 100, True)
        self.zeromq_pub_sink_iq = zeromq.pub_sink(gr.sizeof_gr_complex, 1, "tcp://127.0.0.1:" + str(iq_tx_port), 100, False, (-1), '', True, True)
        self.root_raised_cosine_filter_tx = filter.interp_fir_filter_ccf(
            sps,
            firdes.root_raised_cosine(
                sps,
                samp_rate,
                (samp_rate/float(sps)),
                alpha,
                (11*sps+1)))
        self.root_raised_cosine_filter_0 = filter.fir_filter_ccf(
            1,
            firdes.root_raised_cosine(
                1,
                samp_rate,
                (samp_rate/float(sps)),
                alpha,
                (11*sps+1)))
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
        self.pdu_pdu_to_tagged_stream_0_0 = pdu.pdu_to_tagged_stream(gr.types.complex_t, 'packet_len')
        self.pdu_pdu_filter_0_0 = pdu.pdu_filter(pmt.intern("dst_addr"), pmt.from_uint64(0), False)
        self.pdu_pdu_filter_0 = pdu.pdu_filter(pmt.intern("dst_addr"), pmt.from_uint64(tl_local_addr), False)
        self.epy_block_5 = epy_block_5.blk(max_chars=120)
        self.epy_block_4 = epy_block_4.blk(prime=PRIME, private_key=PRIVATE_KEY, skip_bytes=8)
        self.epy_block_3 = epy_block_3.blk(prime=PRIME, key=PUBLIC_KEY_PEER, skip_bytes=8)
        self.epy_block_2 = epy_block_2.blk()
        self.epy_block_1 = epy_block_1.blk(m=tl_m, rto_ms=tl_rto_ms, node_role=tl_role, mtu_bytes=tl_mtu, local_addr=tl_local_addr, local_port=tl_local_port, max_retries=tl_max_retries)
        self.epy_block_0_0_0 = epy_block_0_0_0.PacketDeframerRX(peer_id=MY_ID, max_bit_errors=1, max_payload_len=8192, sym_rate=samp_rate/sps, snr_up_db=16.0, snr_down_db=13.0, snr_avg=0.2, verbose=1)
        self.epy_block_0_0 = epy_block_0_0.PacketFramerTX(peer_id=tl_peer_addr, preamble_len=1024, repeat_count=1, max_payload_len=8192, preamble_byte=0xFF, postamble_len=160, mcs_mode=MCS_MODE, feedback_timeout=30.0, qpsk_min_len=32, fallback_s=1.0)
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
        self.digital_costas_loop_cc_0 = digital.costas_loop_cc(costas_bw, 4, False)
        self.channels_channel_model_0 = channels.channel_model(
            noise_voltage=nv,
            frequency_offset=0.0,
            epsilon=1.0,
            taps=[1.0],
            noise_seed=0,
            block_tags=False)
        self.blocks_tag_gate_0_0 = blocks.tag_gate(gr.sizeof_gr_complex * 1, False)
        self.blocks_tag_gate_0_0.set_single_key("")
        self.blocks_message_debug_1 = blocks.message_debug(True, gr.log_levels.info)
        self.blocks_message_debug_0 = blocks.message_debug(True, gr.log_levels.info)
        self.blocks_copy_1_2 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_2.set_enabled(True)
        self.blocks_copy_1_1 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_1.set_enabled(True)
        self.blocks_copy_0_0 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_0_0.set_enabled(True)
        self.analog_agc_xx_0 = analog.agc_cc((1e-3), 1.0, 1.0, 1000)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.epy_block_0, 'pdus'), (self.pdu_pdu_filter_0, 'pdus'))
        self.msg_connect((self.epy_block_0, 'pdus'), (self.pdu_pdu_filter_0_0, 'pdus'))
        self.msg_connect((self.epy_block_0_0, 'pdu_out'), (self.pdu_pdu_to_tagged_stream_0_0, 'pdus'))
        self.msg_connect((self.epy_block_0_0_0, 'pdu_out'), (self.blocks_message_debug_0, 'print_pdu'))
        self.msg_connect((self.epy_block_0_0_0, 'pdu_out'), (self.epy_block_0, 'pdus'))
        self.msg_connect((self.epy_block_0_0_0, 'link_out'), (self.epy_block_0_0, 'link_in'))
        self.msg_connect((self.epy_block_1, 'pdu_out'), (self.epy_block_2, 'pdus'))
        self.msg_connect((self.epy_block_1, 'app_out'), (self.epy_block_4, 'pdus_in'))
        self.msg_connect((self.epy_block_2, 'pdus'), (self.blocks_message_debug_1, 'print_pdu'))
        self.msg_connect((self.epy_block_2, 'pdus'), (self.epy_block_0_0, 'msg_in'))
        self.msg_connect((self.epy_block_3, 'pdus_out'), (self.epy_block_1, 'app_in'))
        self.msg_connect((self.epy_block_4, 'pdus_out'), (self.epy_block_5, 'pdu_in'))
        self.msg_connect((self.epy_block_4, 'pdus_out'), (self.zeromq_push_msg_sink_0, 'in'))
        self.msg_connect((self.pdu_pdu_filter_0, 'pdus'), (self.epy_block_1, 'pdu_in'))
        self.msg_connect((self.pdu_pdu_filter_0_0, 'pdus'), (self.epy_block_1, 'pdu_in'))
        self.msg_connect((self.zeromq_pull_msg_source_0, 'out'), (self.blocks_message_debug_0, 'print_pdu'))
        self.msg_connect((self.zeromq_pull_msg_source_0, 'out'), (self.epy_block_3, 'pdus_in'))
        self.connect((self.analog_agc_xx_0, 0), (self.channels_channel_model_0, 0))
        self.connect((self.blocks_copy_0_0, 0), (self.root_raised_cosine_filter_tx, 0))
        self.connect((self.blocks_copy_1_1, 0), (self.analog_agc_xx_0, 0))
        self.connect((self.blocks_copy_1_2, 0), (self.root_raised_cosine_filter_0, 0))
        self.connect((self.blocks_tag_gate_0_0, 0), (self.zeromq_pub_sink_iq, 0))
        self.connect((self.channels_channel_model_0, 0), (self.digital_fll_band_edge_cc_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.epy_block_0_0_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_const_sink_x_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_const_sink_x_1, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.qtgui_freq_sink_x_1, 0))
        self.connect((self.digital_fll_band_edge_cc_0, 0), (self.blocks_copy_1_2, 0))
        self.connect((self.digital_linear_equalizer_0, 0), (self.digital_costas_loop_cc_0, 0))
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.digital_linear_equalizer_0, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0_0, 0), (self.blocks_copy_0_0, 0))
        self.connect((self.root_raised_cosine_filter_0, 0), (self.digital_symbol_sync_xx_0, 0))
        self.connect((self.root_raised_cosine_filter_tx, 0), (self.blocks_tag_gate_0_0, 0))
        self.connect((self.zeromq_sub_source_iq, 0), (self.blocks_copy_1_1, 0))


    def closeEvent(self, event):
        self.settings = Qt.QSettings("gnuradio/flowgraphs", "transeciever_sim")
        self.settings.setValue("geometry", self.saveGeometry())
        self.stop()
        self.wait()

        event.accept()

    def get_tl_peer_addr(self):
        return self.tl_peer_addr

    def set_tl_peer_addr(self, tl_peer_addr):
        self.tl_peer_addr = tl_peer_addr
        self.set_fdd_side(os.environ.get('TL_FDD_SIDE', 'A' if self.tl_local_addr < self.tl_peer_addr else 'B'))
        self.epy_block_0_0.peer_id = self.tl_peer_addr

    def get_tl_local_addr(self):
        return self.tl_local_addr

    def set_tl_local_addr(self, tl_local_addr):
        self.tl_local_addr = tl_local_addr
        self.set_MY_ID(self.tl_local_addr)
        self.set_fdd_side(os.environ.get('TL_FDD_SIDE', 'A' if self.tl_local_addr < self.tl_peer_addr else 'B'))
        self.epy_block_1.local_addr = self.tl_local_addr
        self.pdu_pdu_filter_0.set_val(pmt.from_uint64(self.tl_local_addr))

    def get_fdd_side(self):
        return self.fdd_side

    def set_fdd_side(self, fdd_side):
        self.fdd_side = fdd_side
        self.set_iq_rx_port(53002 if self.fdd_side == 'A' else 53001)
        self.set_iq_tx_port(53001 if self.fdd_side == 'A' else 53002)
        self.set_rx_freq(self.TX_FREQ_B if self.fdd_side == 'A' else self.TX_FREQ_A)
        self.set_tx_freq(self.TX_FREQ_A if self.fdd_side == 'A' else self.TX_FREQ_B)

    def get_TX_FREQ_B(self):
        return self.TX_FREQ_B

    def set_TX_FREQ_B(self, TX_FREQ_B):
        self.TX_FREQ_B = TX_FREQ_B
        self.set_rx_freq(self.TX_FREQ_B if self.fdd_side == 'A' else self.TX_FREQ_A)
        self.set_tx_freq(self.TX_FREQ_A if self.fdd_side == 'A' else self.TX_FREQ_B)

    def get_TX_FREQ_A(self):
        return self.TX_FREQ_A

    def set_TX_FREQ_A(self, TX_FREQ_A):
        self.TX_FREQ_A = TX_FREQ_A
        self.set_rx_freq(self.TX_FREQ_B if self.fdd_side == 'A' else self.TX_FREQ_A)
        self.set_tx_freq(self.TX_FREQ_A if self.fdd_side == 'A' else self.TX_FREQ_B)

    def get_BPSK_CONST(self):
        return self.BPSK_CONST

    def set_BPSK_CONST(self, BPSK_CONST):
        self.BPSK_CONST = BPSK_CONST

    def get_tx_freq(self):
        return self.tx_freq

    def set_tx_freq(self, tx_freq):
        self.tx_freq = tx_freq

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
        self.epy_block_0_0_0.sym_rate = self.samp_rate/self.sps
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))
        self.root_raised_cosine_filter_tx.set_taps(firdes.root_raised_cosine(self.sps, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.epy_block_0_0_0.sym_rate = self.samp_rate/self.sps
        self.qtgui_freq_sink_x_1.set_frequency_range(0, self.samp_rate)
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))
        self.root_raised_cosine_filter_tx.set_taps(firdes.root_raised_cosine(self.sps, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_rx_freq(self):
        return self.rx_freq

    def set_rx_freq(self, rx_freq):
        self.rx_freq = rx_freq

    def get_nv(self):
        return self.nv

    def set_nv(self, nv):
        self.nv = nv
        self.channels_channel_model_0.set_noise_voltage(self.nv)

    def get_iq_tx_port(self):
        return self.iq_tx_port

    def set_iq_tx_port(self, iq_tx_port):
        self.iq_tx_port = iq_tx_port

    def get_iq_rx_port(self):
        return self.iq_rx_port

    def set_iq_rx_port(self, iq_rx_port):
        self.iq_rx_port = iq_rx_port

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
        self.root_raised_cosine_filter_tx.set_taps(firdes.root_raised_cosine(self.sps, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

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

    def get_PUBLIC_KEY_PEER(self):
        return self.PUBLIC_KEY_PEER

    def set_PUBLIC_KEY_PEER(self, PUBLIC_KEY_PEER):
        self.PUBLIC_KEY_PEER = PUBLIC_KEY_PEER
        self.epy_block_3.key = self.PUBLIC_KEY_PEER

    def get_PUBLIC_KEY_MINE(self):
        return self.PUBLIC_KEY_MINE

    def set_PUBLIC_KEY_MINE(self, PUBLIC_KEY_MINE):
        self.PUBLIC_KEY_MINE = PUBLIC_KEY_MINE

    def get_PRIVATE_KEY(self):
        return self.PRIVATE_KEY

    def set_PRIVATE_KEY(self, PRIVATE_KEY):
        self.PRIVATE_KEY = PRIVATE_KEY
        self.epy_block_4.private_key = self.PRIVATE_KEY

    def get_PRIME(self):
        return self.PRIME

    def set_PRIME(self, PRIME):
        self.PRIME = PRIME
        self.epy_block_3.prime = self.PRIME
        self.epy_block_4.prime = self.PRIME

    def get_MY_ID(self):
        return self.MY_ID

    def set_MY_ID(self, MY_ID):
        self.MY_ID = MY_ID
        self.epy_block_0_0_0.peer_id = self.MY_ID

    def get_MCS_MODE(self):
        return self.MCS_MODE

    def set_MCS_MODE(self, MCS_MODE):
        self.MCS_MODE = MCS_MODE
        self._MCS_MODE_callback(self.MCS_MODE)
        self.epy_block_0_0.mcs_mode = self.MCS_MODE

    def get_CH_GAIN(self):
        return self.CH_GAIN

    def set_CH_GAIN(self, CH_GAIN):
        self.CH_GAIN = CH_GAIN

    def get_ADDR(self):
        return self.ADDR

    def set_ADDR(self, ADDR):
        self.ADDR = ADDR




def main(top_block_cls=transeciever_sim, options=None):

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
