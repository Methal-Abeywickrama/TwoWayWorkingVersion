import sys, os
from gnuradio import gr

class blk(gr.basic_block):
    def __init__(self):
        gr.basic_block.__init__(self, name="Path Checker", in_sig=None, out_sig=None)
        print("GRC Python Executable:", sys.executable)
        print("GRC sys.path:", sys.path)
