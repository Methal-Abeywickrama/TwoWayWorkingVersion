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
from gnuradio import eng_notation
from gnuradio.filter import firdes
from gnuradio import gr
from gnuradio.fft import window
import sys
import signal
from PyQt5 import Qt
from argparse import ArgumentParser
from gnuradio.eng_arg import eng_float, intx
from gnuradio import gr, pdu
from gnuradio import iio
import numpy as np
import os
import sip
import threading
import time
import transeciever_epy_arq as epy_arq  # embedded python block
import transeciever_epy_block_0_0_0 as epy_block_0_0_0  # embedded python block
import transeciever_epy_block_0_0_1 as epy_block_0_0_1  # embedded python block
import transeciever_epy_block_corr as epy_block_corr  # embedded python block
import transeciever_epy_csma as epy_csma  # embedded python block
import transeciever_epy_noise as epy_noise  # embedded python block
import transeciever_epy_udp as epy_udp  # embedded python block



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
        self.TX_FREQ_A = TX_FREQ_A = int(1250e6)
        self.TRAIN_BITS = TRAIN_BITS = np.unpackbits(np.frombuffer(bytes([0xF8, 0xDD, 0x42, 0x58]), np.uint8))
        self.RC_X = RC_X = np.arange(-8, 9)/2.0
        self.TRAIN_SYMS = TRAIN_SYMS = list(map(complex, 1 - 2*np.bitwise_xor.accumulate(TRAIN_BITS).astype(int)))
        self.RX_AMP = RX_AMP = 1.0
        self.RC_PULSE = RC_PULSE = np.sinc(RC_X)*np.cos(np.pi*alpha*RC_X)/(1-(2*alpha*RC_X)**2)
        self.FREQ = FREQ = TX_FREQ_A
        self.BPSK_CONST = BPSK_CONST = digital.constellation_rect([1+0j, -1+0j], [0, 1],
        2, 2, 1, 1, 1).base()
        self.train_delay = train_delay = 0
        self.sym_bw = sym_bw = 0.020
        self.sps = sps = 4
        self.samp_rate = samp_rate = 1e6
        self.power_threshold = power_threshold = -40
        self.max_payload_len = max_payload_len = 2048
        self.margin_db = margin_db = 6
        self.fll_loop_bw = fll_loop_bw = 0.01
        self.cs_margin = cs_margin = 6
        self.costas_bw = costas_bw = 0.06
        self.corr_thr = corr_thr = 0.7
        self.adpt_alg = adpt_alg = digital.adaptive_algorithm_nlms( BPSK_CONST, 0.5).base()
        self.TX_MUTE = TX_MUTE = 1
        self.TX_FREQ_B = TX_FREQ_B = int(1270e6)
        self.TX_FREQ = TX_FREQ = FREQ
        self.TRAIN_REF = TRAIN_REF = list(map(complex, RX_AMP*np.convolve(np.kron(TRAIN_SYMS, [1, 0]), RC_PULSE)[8:8+2*len(TRAIN_SYMS)-1]))
        self.SDR_CF = SDR_CF = 433e6
        self.RX_FREQ = RX_FREQ = FREQ
        self.QPSK_CONST = QPSK_CONST = digital.constellation_rect([-1-1j, -1+1j, 1+1j, 1-1j], [0, 1, 3, 2],
        4, 2, 2, 1, 1).base()
        self.MY_ID = MY_ID = int(os.environ.get('CDP_MY_ID', '0'))
        self.FILTER_BW = FILTER_BW = int(1e6)
        self.FEC = FEC = 31
        self.DEST_ID = DEST_ID = int(os.environ.get('CDP_DEST', '255'))
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
        self._cs_margin_range = qtgui.Range(3, 15, 0.5, 6, 200)
        self._cs_margin_win = qtgui.RangeWidget(self._cs_margin_range, self.set_cs_margin, "CSMA sense margin (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._cs_margin_win)
        self._corr_thr_range = qtgui.Range(0.3, 0.98, 0.01, 0.7, 200)
        self._corr_thr_win = qtgui.RangeWidget(self._corr_thr_range, self.set_corr_thr, "'corr_thr'", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._corr_thr_win)
        _TX_MUTE_check_box = Qt.QCheckBox("Mute TX when idle")
        self._TX_MUTE_choices = {True: 1, False: 0}
        self._TX_MUTE_choices_inv = dict((v,k) for k,v in self._TX_MUTE_choices.items())
        self._TX_MUTE_callback = lambda i: Qt.QMetaObject.invokeMethod(_TX_MUTE_check_box, "setChecked", Qt.Q_ARG("bool", self._TX_MUTE_choices_inv[i]))
        self._TX_MUTE_callback(self.TX_MUTE)
        _TX_MUTE_check_box.stateChanged.connect(lambda i: self.set_TX_MUTE(self._TX_MUTE_choices[bool(i)]))
        self.top_layout.addWidget(_TX_MUTE_check_box)
        self._DEST_ID_tool_bar = Qt.QToolBar(self)
        self._DEST_ID_tool_bar.addWidget(Qt.QLabel("Dest ID (255 = all)" + ": "))
        self._DEST_ID_line_edit = Qt.QLineEdit(str(self.DEST_ID))
        self._DEST_ID_tool_bar.addWidget(self._DEST_ID_line_edit)
        self._DEST_ID_line_edit.editingFinished.connect(
            lambda: self.set_DEST_ID(int(str(self._DEST_ID_line_edit.text()))))
        self.top_layout.addWidget(self._DEST_ID_tool_bar)
        self._CH_GAIN_range = qtgui.Range(0, 89.75, 0.25, 20.0, 200)
        self._CH_GAIN_win = qtgui.RangeWidget(self._CH_GAIN_range, self.set_CH_GAIN, "TX attenuation while sending (dB)", "counter_slider", float, QtCore.Qt.Horizontal)
        self.top_layout.addWidget(self._CH_GAIN_win)
        self.root_raised_cosine_filter_0 = filter.fir_filter_ccf(
            1,
            firdes.root_raised_cosine(
                1,
                samp_rate,
                (samp_rate/float(sps)),
                alpha,
                (11*sps+1)))
        self.qtgui_edit_box_msg_0_0 = qtgui.edit_box_msg(qtgui.STRING, "MESSAGE", "TX Message (@N text = to node N, @all text = everyone)", False, False, '', None)
        self._qtgui_edit_box_msg_0_0_win = sip.wrapinstance(self.qtgui_edit_box_msg_0_0.qwidget(), Qt.QWidget)
        self.top_layout.addWidget(self._qtgui_edit_box_msg_0_0_win)
        self.pdu_pdu_to_tagged_stream_0_0 = pdu.pdu_to_tagged_stream(gr.types.byte_t, 'packet_len')
        self.iio_pluto_source_0 = iio.fmcomms2_source_fc32(ADDR if ADDR else iio.get_pluto_uri(), [True, True], BUFFER_SIZE)
        self.iio_pluto_source_0.set_len_tag_key('packet_len')
        self.iio_pluto_source_0.set_frequency(RX_FREQ)
        self.iio_pluto_source_0.set_samplerate(int(samp_rate))
        self.iio_pluto_source_0.set_gain_mode(0, 'manual')
        self.iio_pluto_source_0.set_gain(0, 64)
        self.iio_pluto_source_0.set_quadrature(True)
        self.iio_pluto_source_0.set_rfdc(True)
        self.iio_pluto_source_0.set_bbdc(True)
        self.iio_pluto_source_0.set_filter_params('Auto', '', 0, 0)
        self.iio_pluto_sink_0_0 = iio.fmcomms2_sink_fc32(ADDR if ADDR else iio.get_pluto_uri(), [True, True], BUFFER_SIZE, False)
        self.iio_pluto_sink_0_0.set_len_tag_key('')
        self.iio_pluto_sink_0_0.set_bandwidth(FILTER_BW)
        self.iio_pluto_sink_0_0.set_frequency(TX_FREQ)
        self.iio_pluto_sink_0_0.set_samplerate(int(samp_rate))
        self.iio_pluto_sink_0_0.set_attenuation(0, CH_GAIN)
        self.iio_pluto_sink_0_0.set_filter_params('Auto', '', 0, 0)
        self.epy_udp = epy_udp.UdpTransport(src_id=MY_ID, listen_port=int(os.environ.get('CDP_UDP_PORT', '5200')), deliver_port=int(os.environ.get('CDP_UDP_OUT_PORT', '5201')), bind_addr=os.environ.get('CDP_BIND', '127.0.0.1'), deliver_addr=os.environ.get('CDP_UDP_DELIVER', '127.0.0.1'), max_payload_len=max_payload_len, verbose=1, stats_every_s=10.0)
        self.epy_csma = epy_csma.CsmaMac(samp_rate=samp_rate, samples_per_byte=8*sps, slot_s=0.015, difs_s=0.020, cw_min=8, cw_max=128, cs_margin_db=cs_margin, cs_hyst_db=2.0, cs_bw_hz=400e3, dc_exclude_hz=10e3, nfft=256, sense_s=0.001, floor_window_s=2.0, floor_percentile=10.0, floor_rise_db_s=1.0, echo_timeout_s=0.100, tail_s=0.002, blank_rx=1, max_queue=64, max_defer_s=0.0, verbose=1, stats_every_s=10.0, gate_mode=1, tx_delay_s=0.012, auto_delay=1, gate_guard_s=0.005, tx_mute=TX_MUTE, iio_uri=ADDR, tx_att_db=CH_GAIN, mute_att_db=89.75, unmute_timeout_s=0.1, echo_margin_db=25.0)
        self.epy_block_corr = epy_block_corr.TrainCorr(ref=TRAIN_REF, thr=corr_thr, mark_delay=train_delay, verbose=1)
        self.epy_block_0_0_1 = epy_block_0_0_1.PacketFramerTX(src_id=MY_ID, dest_id=DEST_ID, preamble_len=128, repeat_count=1, max_payload_len=max_payload_len, preamble_byte=0xFF, buffer_size=BUFFER_SIZE, postamble_byte=0xFF, train_enable=1, fec=FEC, interleave=True, samples_per_byte=8*sps)
        self.epy_block_0_0_0 = epy_block_0_0_0.PacketDeframerRX(src_id=MY_ID, max_bit_errors=1, max_payload_len=max_payload_len, bit_rate=375000, fec=FEC, interleave=True, accept_all=0, debug=1, max_dbg_lines=20, stats_every_s=10.0, dup_window_s=2.0)
        self.epy_arq = epy_arq.ArqTransport(src_id=MY_ID, port=int(os.environ.get('CDP_ARQ_PORT', '5100')), bind_addr=os.environ.get('CDP_BIND', '127.0.0.1'), mss=int(os.environ.get('CDP_ARQ_MSS', '1000')), window=int(os.environ.get('CDP_ARQ_WINDOW', '8')), ack_every=4, ack_delay_s=0.5, init_rto_s=2.0, min_rto_s=1.0, max_rto_s=8.0, max_retries=int(os.environ.get('CDP_ARQ_RETRIES', '8')), max_msg_bytes=4194304, max_queue_bytes=8388608, rx_keep_msgs=1000, verbose=1, stats_every_s=10.0)
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
        self.digital_linear_equalizer_0 = digital.linear_equalizer(7, 2, adpt_alg, False, TRAIN_SYMS, "corr_est")
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
        self.dc_blocker_xx_0 = filter.dc_blocker_cc(128, True)
        self.blocks_unpack_k_bits_bb_0_0 = blocks.unpack_k_bits_bb(1)
        self.blocks_tag_gate_0_0 = blocks.tag_gate(gr.sizeof_gr_complex * 1, False)
        self.blocks_tag_gate_0_0.set_single_key("")
        self.blocks_copy_1_2 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_2.set_enabled(True)
        self.blocks_copy_1_0_0 = blocks.copy(gr.sizeof_gr_complex*1)
        self.blocks_copy_1_0_0.set_enabled(True)
        self.blocks_copy_0_0 = blocks.copy(gr.sizeof_char*1)
        self.blocks_copy_0_0.set_enabled(True)
        self.analog_pwr_squelch_xx_0_0 = analog.pwr_squelch_cc(power_threshold, (1e-3), 0, True)
        self.analog_agc_xx_0 = analog.agc_cc((5e-3), 1.0, 1.0, 1000)


        ##################################################
        # Connections
        ##################################################
        self.msg_connect((self.epy_arq, 'pdu_out'), (self.epy_block_0_0_1, 'msg_in'))
        self.msg_connect((self.epy_block_0_0_0, 'pdu_out'), (self.epy_arq, 'pdu_in'))
        self.msg_connect((self.epy_block_0_0_0, 'pdu_out'), (self.epy_udp, 'pdu_in'))
        self.msg_connect((self.epy_block_0_0_1, 'pdu_out'), (self.epy_csma, 'pdu_in'))
        self.msg_connect((self.epy_csma, 'pdu_out'), (self.pdu_pdu_to_tagged_stream_0_0, 'pdus'))
        self.msg_connect((self.epy_udp, 'pdu_out'), (self.epy_block_0_0_1, 'msg_in'))
        self.msg_connect((self.qtgui_edit_box_msg_0_0, 'msg'), (self.epy_block_0_0_1, 'msg_in'))
        self.connect((self.analog_agc_xx_0, 0), (self.blocks_copy_1_0_0, 0))
        self.connect((self.analog_pwr_squelch_xx_0_0, 0), (self.analog_agc_xx_0, 0))
        self.connect((self.blocks_copy_0_0, 0), (self.digital_constellation_modulator_0_0_0, 0))
        self.connect((self.blocks_copy_1_0_0, 0), (self.digital_fll_band_edge_cc_0, 0))
        self.connect((self.blocks_copy_1_2, 0), (self.root_raised_cosine_filter_0, 0))
        self.connect((self.blocks_tag_gate_0_0, 0), (self.iio_pluto_sink_0_0, 0))
        self.connect((self.blocks_unpack_k_bits_bb_0_0, 0), (self.epy_block_0_0_0, 0))
        self.connect((self.dc_blocker_xx_0, 0), (self.epy_csma, 0))
        self.connect((self.dc_blocker_xx_0, 0), (self.epy_noise, 0))
        self.connect((self.digital_constellation_decoder_cb_0_0, 0), (self.digital_diff_decoder_bb_0_0, 0))
        self.connect((self.digital_constellation_modulator_0_0_0, 0), (self.blocks_tag_gate_0_0, 0))
        self.connect((self.digital_costas_loop_cc_0, 0), (self.digital_constellation_decoder_cb_0_0, 0))
        self.connect((self.digital_diff_decoder_bb_0_0, 0), (self.blocks_unpack_k_bits_bb_0_0, 0))
        self.connect((self.digital_fll_band_edge_cc_0, 0), (self.blocks_copy_1_2, 0))
        self.connect((self.digital_linear_equalizer_0, 0), (self.digital_costas_loop_cc_0, 0))
        self.connect((self.digital_symbol_sync_xx_0, 0), (self.epy_block_corr, 0))
        self.connect((self.epy_block_corr, 0), (self.digital_linear_equalizer_0, 0))
        self.connect((self.epy_csma, 0), (self.analog_pwr_squelch_xx_0_0, 0))
        self.connect((self.iio_pluto_source_0, 0), (self.dc_blocker_xx_0, 0))
        self.connect((self.pdu_pdu_to_tagged_stream_0_0, 0), (self.blocks_copy_0_0, 0))
        self.connect((self.root_raised_cosine_filter_0, 0), (self.digital_symbol_sync_xx_0, 0))


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

    def get_TX_FREQ_A(self):
        return self.TX_FREQ_A

    def set_TX_FREQ_A(self, TX_FREQ_A):
        self.TX_FREQ_A = TX_FREQ_A
        self.set_FREQ(self.TX_FREQ_A)

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

    def get_FREQ(self):
        return self.FREQ

    def set_FREQ(self, FREQ):
        self.FREQ = FREQ
        self.set_RX_FREQ(self.FREQ)
        self.set_TX_FREQ(self.FREQ)

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
        self.epy_block_0_0_1.samples_per_byte = 8*self.sps
        self.epy_csma.samples_per_byte = 8*self.sps
        self.root_raised_cosine_filter_0.set_taps(firdes.root_raised_cosine(1, self.samp_rate, (self.samp_rate/float(self.sps)), self.alpha, (11*self.sps+1)))

    def get_samp_rate(self):
        return self.samp_rate

    def set_samp_rate(self, samp_rate):
        self.samp_rate = samp_rate
        self.epy_csma.samp_rate = self.samp_rate
        self.epy_noise.samp_rate = self.samp_rate
        self.iio_pluto_sink_0_0.set_samplerate(int(self.samp_rate))
        self.iio_pluto_source_0.set_samplerate(int(self.samp_rate))
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
        self.epy_block_0_0_0.max_payload_len = self.max_payload_len
        self.epy_block_0_0_1.max_payload_len = self.max_payload_len

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

    def get_cs_margin(self):
        return self.cs_margin

    def set_cs_margin(self, cs_margin):
        self.cs_margin = cs_margin
        self.epy_csma.cs_margin_db = self.cs_margin

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

    def get_TX_MUTE(self):
        return self.TX_MUTE

    def set_TX_MUTE(self, TX_MUTE):
        self.TX_MUTE = TX_MUTE
        self._TX_MUTE_callback(self.TX_MUTE)
        self.epy_csma.tx_mute = self.TX_MUTE

    def get_TX_FREQ_B(self):
        return self.TX_FREQ_B

    def set_TX_FREQ_B(self, TX_FREQ_B):
        self.TX_FREQ_B = TX_FREQ_B

    def get_TX_FREQ(self):
        return self.TX_FREQ

    def set_TX_FREQ(self, TX_FREQ):
        self.TX_FREQ = TX_FREQ
        self.iio_pluto_sink_0_0.set_frequency(self.TX_FREQ)

    def get_TRAIN_REF(self):
        return self.TRAIN_REF

    def set_TRAIN_REF(self, TRAIN_REF):
        self.TRAIN_REF = TRAIN_REF

    def get_SDR_CF(self):
        return self.SDR_CF

    def set_SDR_CF(self, SDR_CF):
        self.SDR_CF = SDR_CF

    def get_RX_FREQ(self):
        return self.RX_FREQ

    def set_RX_FREQ(self, RX_FREQ):
        self.RX_FREQ = RX_FREQ
        self.iio_pluto_source_0.set_frequency(self.RX_FREQ)

    def get_QPSK_CONST(self):
        return self.QPSK_CONST

    def set_QPSK_CONST(self, QPSK_CONST):
        self.QPSK_CONST = QPSK_CONST

    def get_MY_ID(self):
        return self.MY_ID

    def set_MY_ID(self, MY_ID):
        self.MY_ID = MY_ID
        self.epy_arq.src_id = self.MY_ID
        self.epy_block_0_0_0.src_id = self.MY_ID
        self.epy_block_0_0_1.src_id = self.MY_ID
        self.epy_udp.src_id = self.MY_ID

    def get_FILTER_BW(self):
        return self.FILTER_BW

    def set_FILTER_BW(self, FILTER_BW):
        self.FILTER_BW = FILTER_BW
        self.iio_pluto_sink_0_0.set_bandwidth(self.FILTER_BW)

    def get_FEC(self):
        return self.FEC

    def set_FEC(self, FEC):
        self.FEC = FEC
        self.epy_block_0_0_0.fec = self.FEC
        self.epy_block_0_0_1.fec = self.FEC

    def get_DEST_ID(self):
        return self.DEST_ID

    def set_DEST_ID(self, DEST_ID):
        self.DEST_ID = DEST_ID
        Qt.QMetaObject.invokeMethod(self._DEST_ID_line_edit, "setText", Qt.Q_ARG("QString", str(self.DEST_ID)))
        self.epy_block_0_0_1.dest_id = self.DEST_ID

    def get_CH_GAIN(self):
        return self.CH_GAIN

    def set_CH_GAIN(self, CH_GAIN):
        self.CH_GAIN = CH_GAIN
        self.epy_csma.tx_att_db = self.CH_GAIN
        self.iio_pluto_sink_0_0.set_attenuation(0,self.CH_GAIN)

    def get_BUFFER_SIZE(self):
        return self.BUFFER_SIZE

    def set_BUFFER_SIZE(self, BUFFER_SIZE):
        self.BUFFER_SIZE = BUFFER_SIZE
        self.epy_block_0_0_1.buffer_size = self.BUFFER_SIZE

    def get_ADDR(self):
        return self.ADDR

    def set_ADDR(self, ADDR):
        self.ADDR = ADDR
        self.epy_csma.iio_uri = self.ADDR




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
