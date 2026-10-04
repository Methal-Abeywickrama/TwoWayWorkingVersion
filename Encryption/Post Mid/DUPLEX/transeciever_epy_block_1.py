import numpy as np
from gnuradio import gr
import pmt

class blk(gr.basic_block):
    """
    Modular Exponentiation Decryption Block (PDU-based)
    Performs: plaintext_byte = (ciphertext_byte ** private_key) % prime
    """

    def __init__(self, prime=257, private_key=171):
        gr.basic_block.__init__(
            self,
            name='Decrypt Payload',
            in_sig=None,
            out_sig=None
        )
        self.prime = prime
        self.private_key = private_key

        # Register message input and output ports
        self.message_port_register_in(pmt.intern("pdus_in"))
        self.message_port_register_out(pmt.intern("pdus_out"))
        
        # Set message handler function for incoming PDUs
        self.set_msg_handler(pmt.intern("pdus_in"), self.handle_pdu)

    def handle_pdu(self, pdu):
        # Extract metadata dictionary and encrypted payload vector
        meta = pmt.car(pdu)
        payload = pmt.cdr(pdu)

        # Convert PMT uniform vector to NumPy array (uint8 bytes)
        encrypted_data = np.array(pmt.u8vector_elements(payload), dtype=np.uint64)

        # Efficient modular exponentiation: (ciphertext_byte ** private_key) % prime
        decrypt_vec = np.vectorize(lambda x: pow(int(x), self.private_key, self.prime))
        decrypted_data = decrypt_vec(encrypted_data).astype(np.uint8)

        # Convert back to PMT uniform u8vector
        out_payload = pmt.init_u8vector(len(decrypted_data), list(decrypted_data))
        out_pdu = pmt.cons(meta, out_payload)

        # Publish the decrypted PDU
        self.message_port_pub(pmt.intern("pdus_out"), out_pdu)