"""
Embedded Python Block: CSMA/CA MAC Controller
Monitors RX power (magnitude squared) and only allows PDUs to pass
to the TX framer when the channel is clear.
"""
import numpy as np
import pmt
import random
from gnuradio import gr

class CSMA_MAC(gr.sync_block):
    def __init__(self, cca_threshold=0.05, ifs_samples=15000, slot_samples=1500):
        gr.sync_block.__init__(
            self,
            name="CSMA MAC Controller",
            in_sig=[np.float32], # RX signal magnitude squared
            out_sig=None
        )
        self.cca_threshold = float(cca_threshold)
        self.ifs_samples = int(ifs_samples)
        self.slot_samples = int(slot_samples)
        
        self.tx_queue = []
        self.state = "IDLE" 
        self.idle_counter = 0
        self.backoff_counter = 0
        self.be = 1 # Backoff exponent
        
        self.message_port_register_in(pmt.intern("msg_in"))
        self.set_msg_handler(pmt.intern("msg_in"), self.handle_msg)
        self.message_port_register_out(pmt.intern("msg_out"))

    def handle_msg(self, msg):
        self.tx_queue.append(msg)

    def work(self, input_items, output_items):
        rx_power = input_items[0]
        n_samples = len(rx_power)
        
        # Bypass state machine entirely if there is nothing to transmit
        if not self.tx_queue:
            return n_samples
            
        is_busy_array = rx_power > self.cca_threshold
        
        for i in range(n_samples):
            is_busy = is_busy_array[i]
            
            if self.state == "IDLE":
                if self.tx_queue:
                    self.state = "IFS_WAIT"
                    self.idle_counter = 0
            
            elif self.state == "IFS_WAIT":
                if is_busy:
                    self.idle_counter = 0
                    self.state = "BACKOFF_DRAW"
                else:
                    self.idle_counter += 1
                    if self.idle_counter >= self.ifs_samples:
                        self._transmit()
                        
            elif self.state == "BACKOFF_DRAW":
                if not is_busy:
                    slots = random.randint(0, (1 << self.be) - 1)
                    self.backoff_counter = slots * self.slot_samples
                    self.state = "BACKOFF_WAIT"
                    print(f"[CSMA]- channel busy, backing off for {slots} slots",flush = True)
                    
            elif self.state == "BACKOFF_WAIT":
                if is_busy:
                    self.state = "BACKOFF_FREEZE"
                    print(f"[CSMA]- channel busy again, freezing backoff timer",flush = True)
                    
                else:
                    if self.backoff_counter > 0:
                        self.backoff_counter -= 1
                    else:
                        self._transmit()
                        
            elif self.state == "BACKOFF_FREEZE":
                if not is_busy:
                    self.idle_counter += 1
                    if self.idle_counter >= self.ifs_samples:
                        self.idle_counter = 0
                        self.state = "BACKOFF_WAIT"
                        print(f"[CSMA]- channel clear,  Resuming backoff timer",flush = True)

                else:
                    self.idle_counter = 0
                    
        return n_samples

    def _transmit(self):
        if self.tx_queue:
            msg = self.tx_queue.pop(0)
            self.message_port_pub(pmt.intern("msg_out"), msg)
            print(f"[CSMA]- channel clear, transmitting packet",flush = True)
            self.state = "IDLE"
