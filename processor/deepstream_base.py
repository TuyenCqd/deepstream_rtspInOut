import pyds
import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

class BasePipeline:
    def __init__(self, uris):
        self.uris = uris
        self.number_sources = len(uris)
        self.pipeline = Gst.Pipeline()
        self.loop = GLib.MainLoop()

    def create_element(self, factory_name, name, properties={}):
        elm = Gst.ElementFactory.make(factory_name, name)
        if not elm:
            print(f"Unable to create element {name}")
            return None
            
        for key, val in properties.items():
            if key == "caps" and isinstance(val, str):
                val = Gst.Caps.from_string(val)
            elm.set_property(key, val)
        return elm


    def decodebin_child_added(self, child_proxy, Object, name, user_data):
        if name.find("decodebin") != -1:
            Object.connect("child-added", self.decodebin_child_added, user_data)
        if name.find("source") != -1:
            pyds.configure_source_for_ntp_sync(hash(Object))
        if name.find("rtspsrc") != -1:
            Object.set_property("latency", 1000) 
            Object.set_property("drop-on-latency", True)
            # Ép dùng TCP nếu UDP bị mất gói (gây nát hình)
            Object.set_property("protocols", "tcp") 
    def cb_newpad(self, decodebin, decoder_src_pad, data):
        caps = decoder_src_pad.get_current_caps()
        gstname = caps.get_structure(0).get_name()
        source_bin = data
        features = caps.get_features(0)
        if gstname.find("video") != -1:
            if features.contains("memory:NVMM"):
                bin_ghost_pad = source_bin.get_static_pad("src")
                bin_ghost_pad.set_target(decoder_src_pad)

    def create_source_bin(self, index, uri):
        bin_name = f"source-bin-{index:02d}"
        nbin = Gst.Bin.new(bin_name)
        uri_decode_bin = Gst.ElementFactory.make("uridecodebin", f"uri-bin-{index}")
        uri_decode_bin.set_property("uri", uri)
        uri_decode_bin.connect("pad-added", self.cb_newpad, nbin)
        uri_decode_bin.connect("child-added", self.decodebin_child_added, nbin)
        Gst.Bin.add(nbin, uri_decode_bin)
        nbin.add_pad(Gst.GhostPad.new_no_target("src", Gst.PadDirection.SRC))
        return nbin
