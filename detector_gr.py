#!/usr/bin/env python
# vim: set ts=4 sw=4 tw=0 et pm=:
import sys
import math
import numpy
import os.path
import re
import getopt
import time
from functools import partial
from gnuradio import gr
from gnuradio import blocks
import gnuradio.filter
import iridium_toolkit
import osmosdr
import threading
import iq

class burst_sink_c(gr.sync_block):
    def __init__(self, callback, relative_center, relative_bw):
        gr.sync_block.__init__(self,
            name="burst_sink_c",
            in_sig=[numpy.complex64],
            out_sig=[])

        self._bursts = {}
        self._callback = callback
        self._relative_center = relative_center
        self._relative_bw = relative_bw
        self._lower_border = relative_center - relative_bw / 2
        self._upper_border = relative_center + relative_bw / 2
        #print self._relative_center, self._relative_bw

    def work(self, input_items, output_items):
        input = input_items[0]
        n = len(input)
        tags = self.get_tags_in_window(0, 0, n)

        for tag in tags:
            if str(tag.key) == 'gone_burst':
                id = gr.pmt.to_uint64(tag.value)
                if id in self._bursts:
                    self._bursts[id][3] = numpy.append(self._bursts[id][3], input[:tag.offset+1-self.nitems_read(0)])
                    #print "gone burst", id
                    self._callback(self._bursts[id], self._relative_center)
                    del self._bursts[id]

        for burst in self._bursts:
            self._bursts[burst][3] = numpy.append(self._bursts[burst][3], input)

        for tag in tags:
            if str(tag.key) == 'new_burst':
                rel_freq = gr.pmt.to_float(gr.pmt.vector_ref(tag.value, 1))
                if self._lower_border < rel_freq <= self._upper_border:
                    id = gr.pmt.to_uint64(gr.pmt.vector_ref(tag.value, 0))
                    mag = gr.pmt.to_float(gr.pmt.vector_ref(tag.value, 2))
                    self._bursts[id] = [self.nitems_read(0), mag, rel_freq - self._relative_center, numpy.array((), dtype=numpy.complex64)]
                    #print "new burst:", self._relative_center, id, rel_freq
                    self._bursts[id][3] = numpy.append(self._bursts[id][3], input[tag.offset - self.nitems_read(0):])

        return n


class cpdu_sink(gr.sync_block):
    def __init__(self, callback):
        gr.sync_block.__init__(self,
            name="cpdu_sink",
            in_sig=[],
            out_sig=[])

        self._callback = callback
        self.message_port_register_in(gr.pmt.intern('cpdus'))
        self.set_msg_handler(gr.pmt.intern('cpdus'), self.handle_msg)

    def handle_msg(self, msg_pmt):
        meta = gr.pmt.to_python(gr.pmt.car(msg_pmt))
        msg = gr.pmt.cdr(msg_pmt)

        if not gr.pmt.is_c32vector(msg):
            return

        data = gr.pmt.c32vector_elements(msg)

        #{'relative_center': 0.12646484375, 'magnitude': 8.807437896728516, 'offset': 986113L}
        print meta

        self._callback(meta['offset'], meta['burst_relative_center'], 
                meta['span_relative_center'], meta['magnitude'], data)

class Detector(object):
    def __init__(self, sample_rate, decimation, threshold=7.0, verbose=False, signal_width=40e3):
        self._input_sample_rate = sample_rate
        self._verbose = verbose
        self._threshold = threshold

        self._fft_size = int(math.pow(2, 1 + int(math.log(self._input_sample_rate / 1000, 2)))) # fft is approx 1ms long
        self._burst_pre_len = self._fft_size
        self._burst_post_len = 8 * self._fft_size
        self._burst_width= int(signal_width / (self._input_sample_rate / self._fft_size)) # Area to ignore around an already found signal in FFT bins
        if decimation > 1:
            assert decimation % 2 == 0

            self._channels = decimation + 1
            self._pfb_over_sample_ratio = self._channels / (self._channels - 1.)
            self.output_sample_rate = int(round(float(self._input_sample_rate) / self._channels * self._pfb_over_sample_ratio))
            assert self.output_sample_rate == self._input_sample_rate / decimation

            self._fir_bw = (self._input_sample_rate / self._channels + signal_width) / 2
            self._fir_tw = (self.output_sample_rate / 2 - self._fir_bw) * 2

            if self._fir_tw < 0:
                print >> sys.stderr, "PFB over sampling ratio not enough to create a working FIR filter"
                sys.exit(1)

            self._pfb_fir_filter = gnuradio.filter.firdes.low_pass_2(1, self._input_sample_rate, self._fir_bw, self._fir_tw, 60)
            self._use_pfb = True

            if self._verbose:
                print >> sys.stderr, "self._channels", self._channels
                print >> sys.stderr, "self._pfb_over_sample_ratio", self._pfb_over_sample_ratio
                print >> sys.stderr, "self._fir_bw", self._fir_bw
                print >> sys.stderr, "self._fir_tw", self._fir_tw
        else:
            self._use_pfb = False
            self.output_sample_rate = self._input_sample_rate

        if self._verbose:
            print >> sys.stderr, "require %.1f dB" % self._threshold
            print >> sys.stderr, "burst_width: %d (= %.1f Hz)" % (self._burst_width, self._burst_width*self._input_sample_rate/self._fft_size)
            print >> sys.stderr, "output sample rate: %d" % self.output_sample_rate

        self._lock = threading.Lock()

    def process(self, data_collector, filename=None, sample_format=None):
        self._data_collector = data_collector
        self._filename = filename

        if filename.endswith(".conf"):
            import ConfigParser
            config = ConfigParser.ConfigParser()
            config.read(filename)
            items = config.items("osmosdr-source")
            d = {key: value for key, value in items}

            if 'device_args' in d:
                source = osmosdr.source(args=d['device_args'])
            else:
                source = osmosdr.source()

            source.set_sample_rate(int(d['sample_rate']))
            source.set_center_freq(int(d['center_freq']), 0)
            if 'gain' in d:
                source.set_gain(int(d['gain']), 0)
            if 'if_gain' in d:
                source.set_if_gain(int(d['if_gain']), 0)
            if 'bb_gain' in d:
                source.set_bb_gain(int(d['bb_gain']), 0)
            if 'bandwidth' in d:
                source.set_bandwidth(int(d['bandwidth']), 0)
            #source.set_freq_corr($corr0, 0)
            #source.set_dc_offset_mode($dc_offset_mode0, 0)
            #source.set_iq_balance_mode($iq_balance_mode0, 0)
            #source.set_gain_mode($gain_mode0, 0)
            #source.set_antenna($ant0, 0)

            converter = None
        else:
            if sample_format == "rtl":
                converter = iridium_toolkit.iuchar_to_complex()
                itemsize = gr.sizeof_char
            elif sample_format == "hackrf":
                converter = blocks.interleaved_char_to_complex()
                itemsize = gr.sizeof_char
            elif sample_format == "sc16":
                converter = blocks.interleaved_short_to_complex()
                itemsize = gr.sizeof_short
            elif sample_format == "float":
                converter = None
                itemsize = gr.sizeof_gr_complex
            else:
                raise RuntimeError("Unknown sample format for offline mode given")
            source = blocks.file_source(itemsize=itemsize, filename=filename, repeat=False)

        tb = gr.top_block()

        fft_burst_tagger = iridium_toolkit.fft_burst_tagger(fft_size=self._fft_size, threshold=self._threshold,
                                sample_rate=self._input_sample_rate,
                                burst_pre_len=self._burst_pre_len, burst_post_len=self._burst_post_len,
                                burst_width=self._burst_width, debug=self._verbose)

        pdu_collector = cpdu_sink(self._new_burst_cpdu)
        if self._use_pfb:
            pdu_converters = []
            sinks = []

            for channel in range(self._channels):
                center = channel if channel <= self._channels / 2 else (channel - self._channels)

                pdu_converters.append(iridium_toolkit.tagged_burst_to_pdu(100000, center / float(self._channels), 1. / self._channels))

            #sinks2 = [blocks.file_sink(itemsize=gr.sizeof_gr_complex, filename="/tmp/channel-%d.f32"%i) for i in range(self._channels)]

            pfb = gnuradio.filter.pfb.channelizer_ccf(numchans=self._channels, taps=self._pfb_fir_filter, oversample_rate=self._pfb_over_sample_ratio)

            if converter:
                tb.connect(source, converter, fft_burst_tagger, pfb)
            else:
                tb.connect(source, fft_burst_tagger, pfb)

            for i in range(self._channels):
                tb.connect((pfb, i), pdu_converters[i])
                tb.msg_connect((pdu_converters[i], 'cpdus'), (pdu_collector, 'cpdus'))    
                #tb.connect((pfb, i), sinks2[i])
        else:
            sink = burst_sink_c(self._new_burst, 0., 1.)
            if converter:
                tb.connect(source, converter, fft_burst_tagger, sink)
            else:
                tb.connect(source, fft_burst_tagger, sink)
           

        self._si = 0
        tb.run()

    def _new_burst_cpdu(self, offset, burst_relative_center, span_relative_center, magnitude, data):
        self._data_collector(offset / float(self.output_sample_rate), magnitude,
                burst_relative_center * self._input_sample_rate,
                span_relative_center * self._input_sample_rate, data)

    def _new_burst(self, burst, relative_center):
        with self._lock:
            #print "new burst at t=", burst[0] / float(self.output_sample_rate), "f=", burst[2] * self._input_sample_rate
            #print "len:", len(burst[3])
            #iq.write("/tmp/signals/signal-%d.f32" % self._si, burst[3])
            self._si += 1
            self._data_collector(burst[0] / float(self.output_sample_rate), burst[1], burst[2] * self._input_sample_rate, relative_center * self._input_sample_rate, burst[3])
        pass

def file_collector(basename, time_stamp, signal_strength, bin_index, freq, signal):
    filename = "/tmp/bursts/%s-%07d-o%+07d.det" % (os.path.basename(basename), time_stamp, freq)
    signal.tofile(filename)

if __name__ == "__main__":
    options, remainder = getopt.getopt(sys.argv[1:], 'r:d:vf:p:', [
                                                            'rate=',
                                                            'db=',
                                                            'verbose',
                                                            'format=',
                                                            'pipe',
                                                            ])
    sample_rate = None
    verbose = False
    threshold = 8.5 # 8.5 dB over noise
    fmt = None
    pipe = None
    online = False
    filename = None

    for opt, arg in options:
        if opt in ('-r', '--rate'):
            sample_rate = int(arg)
        elif opt in ('-d', '--db'):
            threshold = float(arg)
        elif opt in ('-v', '--verbose'):
            verbose = True
        elif opt in ('-f', '--format'):
            fmt = arg
        elif opt in ('-p', '--pipe'):
            pipe = arg
        elif opt in ('-o', '--online'):
            online = True

    if sample_rate == None:
        print >> sys.stderr, "Sample rate missing!"
        exit(1)

    if fmt == None and not online:
        print >> sys.stderr, "Need to specify the sample format (one of rtl, hackrf, sc16, float) in offline mode"
        exit(1)

    basename=None

    if len(remainder)==0 or pipe !=None:
        if pipe==None:
            print >> sys.stderr, "WARN: pipe mode not set"
            pipe="t"
        basename="i-%.4f-%s1"%(time.time(),pipe)
        print >> sys.stderr, basename
        if not online:
            filename = "/dev/stdin"
    else:
        filename = remainder[0]
        basename = re.sub('\.[^.]*$', '', filename)

    d = Detector(sample_rate, threshold=threshold, verbose=verbose)
    d.process(partial(file_collector, basename), filename, sample_format=fmt)
