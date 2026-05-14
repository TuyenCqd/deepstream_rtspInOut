from pathlib import Path
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst
from config.const import MODEL_DIR, CONFIG_DIR

Gst.init(None)

ELEMENTS = {
    "streammux": {
        "factory_name": "nvstreammux",
        "properties": {
            "width": 1280, "height": 720, "batch-size": 5,
            "live-source": 1,
            "batched-push-timeout": 40000, "nvbuf-memory-type": 0
        }
    },
    "pgie": {
        "factory_name": "nvinfer", 
        "properties": {
            "config-file-path": (Path(MODEL_DIR) / "traffic_rtdetr/config_infer_primary_rtdetr.txt").as_posix()
        }
    },
    "tracker": {
        "factory_name": "nvtracker",
        "properties": {
            "tracker-width": 640,
            "tracker-height": 384,
            "ll-lib-file": "/opt/nvidia/deepstream/deepstream/lib/libnvds_nvmultiobjecttracker.so",
            "ll-config-file": (Path(MODEL_DIR) / "tracker/config_tracker_NvDCF_accuracy.yml").as_posix(),
            "display-tracking-id": 1 # Để tracker ghi ID vào metadata
        }
    },    
    "nvdsanalytics": {
        "factory_name": "nvdsanalytics",
        "properties": {
            "config-file": (Path(CONFIG_DIR) / "config_nvdsanalytics_final.txt").as_posix()
        }
    },
    "tiler": {
        "factory_name": "nvmultistreamtiler",
        "properties": {"width": 1280, "height": 720}
    },
    "nvvidconv": {
        "factory_name": "nvvideoconvert",
        "properties": {}
    },
    "nvosd": {
        "factory_name": "nvdsosd",
        "properties": {
            "process-mode": 1, # 0: CPU, 1: GPU (Thường dùng 0 cho ổn định)
            "display-text": 1  # Để hiển thị text (Label + ID)
        }
    },
    "nvvidconv_tosave": {
        "factory_name": "nvvideoconvert",
        "properties": {}
    },
    "caps_tosave": {
        "factory_name": "capsfilter",
        "properties": {
            # Chuyển về video/x-raw (không có memory:NVMM) để CPU đọc được
            "caps": "video/x-raw, format=RGBA" 
        }
    },
    "nvvidconv_postosd": {
        "factory_name": "nvvideoconvert",
        "properties": {}
    },
    "capsfilter": {
        "factory_name": "capsfilter",
        "properties": {
            "caps": "video/x-raw(memory:NVMM), format=I420"  # Để dạng String ở đây
        }
    },
    "encoder": {
        "factory_name": "nvv4l2h264enc",
        "properties": {
            "bitrate": 4000000,
            "preset-id": 1,
            "insert-sps-pps": 1,
            "iframeinterval": 30,
            # Xóa "bufapi-version"
            # Xóa "maxperf-enable"
            "profile": 0 # 0 là Baseline, giúp giảm trễ tối đa cho RTSP
        }
    },

    "rtppay": {
        "factory_name": "rtph264pay",
        "properties": {}
    },
    "sink": {
        "factory_name": "udpsink",
        "properties": {
            "host": "224.224.224.224",
            "port": 5400,
            "async": False,
            "sync": False,  # ĐỔI THÀNH FALSE
            "qos": 0
        }
    },

}
