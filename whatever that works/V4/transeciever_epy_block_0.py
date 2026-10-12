"""
Embedded Python Block: Burst Pad (fixed-length zero fill)

Pads every modulated burst with ZERO SAMPLES (silence, not modulated bytes) up to a fixed
length, so each burst is exactly as long as the Pluto/ANTSDR sink buffer. The gr-iio sink in
tagged mode only accepts packets whose length equals its buffer_size.

Message ports (PDUs of complex samples):
    pdu_in  : PDU with a c32vector (the modulated frame, length L samples)
    pdu_out : PDU with a c32vector of exactly TARGET samples = frame + (TARGET - L) zeros

TARGET = (preamble_len + train_len + sync 4 + header 6 + max_payload_len + crc 4 + postamble_len) * samples_per_byte
         i.e. the length of the LARGEST frame the framer can produce.
Set the sink's buffer_size to this same number (the block prints it at start-up).

Frames longer than TARGET cannot be sent as one buffer; they are dropped with an error message.
"""
import numpy as np
import pmt
from gnuradio import gr

SYNC_BYTES = 4
HDR_BYTES = 6   # len(2) + dest_id + msg_id + rep + n_reps
CRC_BYTES = 4


def burst_samples(max_payload_len, preamble_len, train_len, samples_per_byte, postamble_len=0):
    """Length in samples of the largest frame the framer can emit."""
    nbytes = (int(preamble_len) + int(train_len) + SYNC_BYTES + HDR_BYTES
              + int(max_payload_len) + CRC_BYTES + int(postamble_len))
    return nbytes * int(samples_per_byte)


def pad_to(samples, target):
    """Return `samples` followed by zeros, exactly `target` long (complex64)."""
    out = np.zeros(int(target), dtype=np.complex64)
    out[:len(samples)] = samples
    return out


class BurstPad(gr.basic_block):
    def __init__(self, max_payload_len=1024, preamble_len=16, train_len=4,
                 samples_per_byte=32, postamble_len=0, verbose=1):
        gr.basic_block.__init__(
            self,
            name="Burst Pad (zero fill)",
            in_sig=None,
            out_sig=None,
        )
        self.max_payload_len = int(max_payload_len)
        self.preamble_len = int(preamble_len)
        self.train_len = int(train_len)
        self.samples_per_byte = int(samples_per_byte)
        self.postamble_len = int(postamble_len)
        self.verbose = int(verbose)

        self.target = burst_samples(self.max_payload_len, self.preamble_len,
                                    self.train_len, self.samples_per_byte, self.postamble_len)
        self.n_sent = 0
        self.n_dropped = 0

        self.message_port_register_in(pmt.intern("pdu_in"))
        self.set_msg_handler(pmt.intern("pdu_in"), self.handle_pdu)
        self.message_port_register_out(pmt.intern("pdu_out"))

        print(f"[PAD] every burst = {self.target} samples "
              f"({self.target // max(self.samples_per_byte, 1)} bytes). "
              f"Set the sink buffer_size to {self.target}.", flush=True)

    def handle_pdu(self, msg):
        if not pmt.is_pair(msg):
            self.n_dropped += 1
            print("[PAD] dropped: message is not a PDU (pair)", flush=True)
            return

        data = pmt.cdr(msg)
        if not pmt.is_c32vector(data):
            self.n_dropped += 1
            print("[PAD] dropped: PDU payload is not a complex (c32) vector", flush=True)
            return

        samples = np.array(pmt.c32vector_elements(data), dtype=np.complex64)
        n = len(samples)
        if n == 0:
            return

        if n > self.target:
            self.n_dropped += 1
            print(f"[PAD] dropped: frame is {n} samples but the fixed burst is only "
                  f"{self.target}. Increase max_payload_len / check preamble_len, train_len, "
                  f"samples_per_byte.", flush=True)
            return

        out = pad_to(samples, self.target)
        vec = pmt.init_c32vector(self.target, out.tolist())
        self.message_port_pub(pmt.intern("pdu_out"), pmt.cons(pmt.PMT_NIL, vec))
        self.n_sent += 1

        if self.verbose:
            print(f"[PAD] burst {self.n_sent}: {n} samples ({n / self.samples_per_byte:.1f} B) "
                  f"+ {self.target - n} zeros = {self.target}", flush=True)