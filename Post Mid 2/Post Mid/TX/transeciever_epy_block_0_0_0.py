"""
Embedded Python Block: PacketDeframerRX
"""
import numpy as np
from gnuradio import gr
import zlib

class PacketDeframerRX(gr.sync_block):
    STATE_SYNC = 0
    STATE_HEADER = 1
    STATE_PAYLOAD_CRC = 2

    def __init__(self, max_bit_errors=1, max_payload_len=512):
        gr.sync_block.__init__(
            self,
            name="Packet Deframer RX",
            in_sig=[np.uint8],
            out_sig=None
        )
        self.max_bit_errors = max_bit_errors
        self.max_payload_len = max_payload_len
        self.sync_target = 0x1ACFFC1D  # Matches TX sync word

        self.state = self.STATE_SYNC
        self.shift_reg = 0
        self.bit_buf = 0
        self.bit_count = 0
        self.byte_buf = bytearray()
        self.payload_len = 0
        self.target_bytes = 0

    def work(self, input_items, output_items):
        in_bits = input_items[0]

        for bit in in_bits:
            b = int(bit) & 1

            if self.state == self.STATE_SYNC:
                self.shift_reg = ((self.shift_reg << 1) | b) & 0xFFFFFFFF
                xor_diff = self.shift_reg ^ self.sync_target
                
                # Check for sync match (allowing bit errors over RF)
                if bin(xor_diff).count('1') <= self.max_bit_errors:
                    self.state = self.STATE_HEADER
                    self.bit_buf = 0
                    self.bit_count = 0
                    self.byte_buf = bytearray()
                    self.target_bytes = 2

            elif self.state == self.STATE_HEADER:
                self.bit_buf = (self.bit_buf << 1) | b
                self.bit_count += 1
                if self.bit_count == 8:
                    self.byte_buf.append(self.bit_buf)
                    self.bit_buf = 0
                    self.bit_count = 0
                    if len(self.byte_buf) == self.target_bytes:
                        self.payload_len = int.from_bytes(self.byte_buf, 'big')
                        if 0 < self.payload_len <= self.max_payload_len:
                            self.state = self.STATE_PAYLOAD_CRC
                            self.byte_buf = bytearray()
                            self.target_bytes = self.payload_len + 4  # payload + 4 CRC bytes
                        else:
                            # False trigger / corrupted header
                            self.state = self.STATE_SYNC

            elif self.state == self.STATE_PAYLOAD_CRC:
                self.bit_buf = (self.bit_buf << 1) | b
                self.bit_count += 1
                if self.bit_count == 8:
                    self.byte_buf.append(self.bit_buf)
                    self.bit_buf = 0
                    self.bit_count = 0
                    if len(self.byte_buf) == self.target_bytes:
                        payload = bytes(self.byte_buf[:self.payload_len])
                        rx_crc = int.from_bytes(self.byte_buf[self.payload_len:], 'big')
                        calc_crc = zlib.crc32(payload)

                        if rx_crc == calc_crc:
                            msg_str = payload.decode('utf-8', errors='replace')
                            print(f"\n========================================", flush=True)
                            print(f"[RX RECEIVED]: {msg_str}", flush=True)
                            print(f"========================================\n", flush=True)
                        else:
                            print(f"[RX CRC ERROR] Expected: {calc_crc:#010x}, Received: {rx_crc:#010x}", flush=True)

                        self.state = self.STATE_SYNC
                        self.shift_reg = 0

        return len(in_bits)
