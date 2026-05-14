import sys
import time
import gi
import os
import pyds
import queue
from loguru import logger
from pathlib import Path
import argparse
from collections import deque, Counter
from datetime import datetime
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer
from processor.deepstream_base import BasePipeline
from config.pipeline import ELEMENTS
# from config.const import CONFIG_DIR, width_frame, height_frame, CONFIG_INPUT
from config.const import CONFIG_DIR, MODEL_DIR
from utils.video_resolution import get_optimal_streammux_resolution
from utils.roi_handler import ROIConfigHandler
from utils.line_handler import LineConfigHandler

logger.add("logs/pipeline_{time}.log", rotation="10 MB", level="INFO")

class Pipeline(BasePipeline): 
    __instance = None 
    def __new__(cls, *args, **kwargs):  # Thêm **kwargs ở đây
        if cls.__instance is None: 
            cls.__instance = super(Pipeline, cls).__new__(cls) 
            cls.__instance.__initialized = False 
        return cls.__instance 
    
    def __init__(self, *args, roi_input=None, line_input=None, udp_port=5400, rtsp_port=9112): 
        if hasattr(self, "__initialized") and self.__initialized:
            return logger.info("Initializing Singleton Pipeline Object Detector...") 
        super().__init__(*args) 

        self.udp_port = udp_port
        self.rtsp_port = rtsp_port

        self.width_frame, self.height_frame = get_optimal_streammux_resolution(self.uris)
        logger.info(f"\n=== Streammux Configuration ===")
        logger.info(f"Resolution: {self.width_frame}x{self.height_frame}")

        self.fps_data = {} 
        self.crossed_objects_in = set()
        self.crossed_objects_out = set()
        self.persons_previously_in_roi = set() 

        self.max_cache_size = 2000
        self.frame_counters = {} 
        self.stream_counts = {}
        self.save_path = "root_alerts" 

        self.obj_encoder = pyds.nvds_obj_enc_create_context(0) 
        self.save_queue = queue.Queue() 
        self.is_running = True 

        self.save_requests = {}
        self.roi_history = {} 

        self.is_roi_enabled = False
        self.is_line_enabled = False

        self.config_path = os.path.join(MODEL_DIR, "traffic_rtdetr/config_infer_primary_rtdetr.txt")
        
        self.final_cfg_path = Path(CONFIG_DIR) / "config_nvdsanalytics_final.txt"
        self.final_cfg_path = Path(CONFIG_DIR) / f"config_analytics_{self.rtsp_port}.txt"

        self.cfg_content = (
            f"[property]\nenable=1\nconfig-width={self.width_frame}\nconfig-height={self.height_frame}\n"
            "osd-mode=1\ndisplay-font-size=12\n\n"
        )

        self.roi_handler = ROIConfigHandler()
        self.line_handler = LineConfigHandler()

        if roi_input:
            self.is_roi_enabled = True
            self.update_person_confidence(self.config_path, self.is_roi_enabled)
            self.roi_handler.read_config(roi_input)
            self.cfg_content += self.roi_handler.get_config_content()
            self.roi_points_cache = {i: stream_data for i, stream_data in enumerate(self.roi_handler.roi_streams)}

        if line_input:
            self.is_line_enabled = True
            self.update_person_confidence(self.config_path, self.is_roi_enabled)
            self.line_handler.read_config(line_input)
            self.cfg_content += self.line_handler.get_config_content()

        self._save_final_config()


        self.__initialized = True 
        logger.info("Pipeline Initialized Successfully!") 
    
    def update_person_confidence(self, config_path, is_roi_mode):
        # Xác định ngưỡng confidence dựa trên tham số đầu vào
        new_conf = 0.35 if is_roi_mode else 0.25
        
        if not os.path.exists(config_path):
            print(f"Lỗi: Không tìm thấy file config tại {config_path}")
            return

        with open(config_path, 'r') as f:
            lines = f.readlines()

        new_lines = []
        in_person_section = False
        threshold_updated = False

        for line in lines:
            strip_line = line.strip()
            
            # Kiểm tra xem có đang ở trong section của class Person (ID 0) không
            if strip_line == "[class-attrs-0]":
                in_person_section = True
            elif strip_line.startswith("[") and strip_line.endswith("]"):
                in_person_section = False
            
            # Nếu đang ở trong section Person và tìm thấy dòng threshold
            if in_person_section and strip_line.startswith("pre-cluster-threshold"):
                new_lines.append(f"pre-cluster-threshold={new_conf}\n")
                threshold_updated = True
            else:
                new_lines.append(line)

        # Trường hợp file chưa có section [class-attrs-0], ta tự thêm vào cuối
        if not threshold_updated:
            new_lines.append("\n[class-attrs-0]\n")
            new_lines.append(f"pre-cluster-threshold={new_conf}\n")
            new_lines.append("nms-iou-threshold=0.45\n")

        # Ghi đè lại file config
        with open(config_path, 'w') as f:
            f.writelines(new_lines)
        
        print(f"--- Đã cập nhật Person Confidence: {new_conf} (Mode: {'ROI' if is_roi_mode else 'LINE'}) ---")

    def _save_final_config(self):
        with open(self.final_cfg_path, "w", encoding="utf-8") as f:
            f.write(self.cfg_content)
        os.chmod(self.final_cfg_path, 0o666)
        logger.info(f" Final config generated at {self.final_cfg_path}")

    def _manage_id_cache(self):
        for cache_set in [self.crossed_objects_in, self.crossed_objects_out, self.persons_previously_in_roi]:
            if len(cache_set) > self.max_cache_size:
                items_to_remove = list(cache_set)[:100] 
                for item in items_to_remove:
                    cache_set.discard(item)

    def _get_fps(self, source_id): 
        current_time = time.time() 
        if source_id not in self.fps_data: 
            self.fps_data[source_id] = [current_time, 0, 0.0] 
        self.fps_data[source_id][1] += 1 
        if self.fps_data[source_id][1] >= 30: 
            elapsed = current_time - self.fps_data[source_id][0] 
            fps = self.fps_data[source_id][1] / elapsed 
            self.fps_data[source_id][2] = fps 
            self.fps_data[source_id][0] = current_time 
            self.fps_data[source_id][1] = 0 
            logger.info(f"Source: {source_id} | FPS: {fps:.2f}") 
        return self.fps_data[source_id][2]
    
    def _get_stable_roi_counts(self, source_id, current_roi_counts):
        now = time.time()
        if source_id not in self.roi_history:
            self.roi_history[source_id] = {}

        stable_counts = {}
        duration = 5  # Cấu hình 10 giây

        for roi_idx, count in current_roi_counts.items():
            if roi_idx not in self.roi_history[source_id]:
                self.roi_history[source_id][roi_idx] = [] # Lưu list các tuple (timestamp, count)
            
            # 1. Thêm dữ liệu mới: (thời gian hiện tại, số lượng người)
            self.roi_history[source_id][roi_idx].append((now, count))
            
            # 2. Loại bỏ các dữ liệu cũ hơn 10 giây (Cleaning)
            self.roi_history[source_id][roi_idx] = [
                item for item in self.roi_history[source_id][roi_idx] 
                if now - item[0] <= duration
            ]
            
            # 3. Lấy giá trị xuất hiện nhiều nhất (Mode) từ các giá trị còn lại
            history_values = [item[1] for item in self.roi_history[source_id][roi_idx]]
            
            if history_values:
                most_common = Counter(history_values).most_common(1)[0][0]
                stable_counts[roi_idx] = most_common
            else:
                stable_counts[roi_idx] = count
                
        return stable_counts
    
    def _add_display_overlay(self, frame_meta, batch_meta, text_content):
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
        display_meta.num_labels = 1
        
        txt = display_meta.text_params[0]
        txt.display_text = text_content
        
        # Cấu hình chung cho cả 2 mode
        txt.x_offset, txt.y_offset = 30, 30
        txt.font_params.font_name = "Serif"
        txt.font_params.font_size = 14
        txt.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
        
        txt.set_bg_clr = 1
        txt.text_bg_clr.set(0.0, 0.0, 0.0, 0.7)
        
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)


    def _set_obj_display(self, obj_meta, text, color, bg_opacity=0.2):
        # 1. Cấu hình Viền (Border)
        obj_meta.rect_params.border_width = 3 
        obj_meta.rect_params.border_color.set(*color)
        
        # 2. Cấu hình Màu nền khối (Background Rect) -
        obj_meta.rect_params.has_bg_color = 1
        obj_meta.rect_params.bg_color.set(color[0], color[1], color[2], bg_opacity)

        # 3. Cấu hình Chữ (Text)
        obj_meta.text_params.display_text = text 
        obj_meta.text_params.font_params.font_size = 10 
        obj_meta.text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0) # Chữ trắng
        
        # 4. Cấu hình Nền của chữ (Text Background)
        obj_meta.text_params.set_bg_clr = 1 
        obj_meta.text_params.text_bg_clr.set(color[0], color[1], color[2], 0.6)


    def _show_dashboard_roi(self, frame_meta, batch_meta, fps_val, num_in_roi):
        source_id = frame_meta.source_id
        roi_list = self.roi_points_cache.get(source_id, [])
        total_target = sum(roi.get("target", 0) for roi in roi_list) if roi_list else 0
        
        content = f"CAM: {source_id} | FPS: {fps_val:.1f} | ROI TOTAL: {num_in_roi}/{total_target}"
        self._add_display_overlay(frame_meta, batch_meta, content)

    def _show_dashboard_line(self, frame_meta, batch_meta, fps_val):
        source_id = frame_meta.source_id
        counts = self.stream_counts.get(source_id, {'in': 0, 'out': 0})
        
        content = f"CAM: {source_id} | FPS: {fps_val:.1f} | IN: {counts['in']} | OUT: {counts['out']}"
        self._add_display_overlay(frame_meta, batch_meta, content)


    def _roi_analytics_probe(self, frame_meta, num_rois):
        # 1. Khởi tạo
        roi_counts = {i: 0 for i in range(num_rois)}
        l_obj = frame_meta.obj_meta_list
        
        while l_obj:
            try:
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data)
                obj_id = obj_meta.object_id
                
                # Reset hiển thị mặc định
                obj_meta.rect_params.border_width = 0
                obj_meta.text_params.display_text = "" 

                if obj_meta.class_id == 0:  # Chỉ xử lý class Người
                    is_now_in_any_roi = False
                    
                    l_user_meta = obj_meta.obj_user_meta_list
                    while l_user_meta:
                        user_meta = pyds.NvDsUserMeta.cast(l_user_meta.data)
                        if user_meta.base_meta.meta_type == pyds.NvDsMetaType.NVDS_OBJ_META_NVDSANALYTICS:
                            analytics_meta = pyds.NvDsAnalyticsObjInfo.cast(user_meta.user_meta_data)
                            
                            # KIỂM TRA VÀ ĐẾM TỪNG VÙNG
                            if analytics_meta.roiStatus:
                                is_now_in_any_roi = True
                                for roi_id in analytics_meta.roiStatus:
                                    try:
                                        # Lấy index từ tên "roi-C00", "roi-C01"...
                                        # Đảm bảo hàm ghi file config đặt tên đúng định dạng này
                                        idx = int(roi_id.split('C')[-1])
                                        if idx in roi_counts:
                                            roi_counts[idx] += 1
                                    except:
                                        continue
                        l_user_meta = l_user_meta.next

                    # Xử lý trạng thái hiển thị của Object
                    if is_now_in_any_roi:
                        self.persons_previously_in_roi.add(obj_id)
                        self._set_obj_display(obj_meta, f"IN ROI {obj_meta.confidence}", (1.0, 0.0, 0.0, 1.0))
                    elif obj_id in self.persons_previously_in_roi:
                        self._set_obj_display(obj_meta, "EXITED", (0.0, 0.0, 1.0, 1.0))
                        
            except StopIteration:
                break
            l_obj = l_obj.next
            
        return roi_counts


    def _draw_roi_area(self, frame_meta, batch_meta, roi_counts, roi_list):
        for i, roi_data in enumerate(roi_list):
            # 1. Kiểm tra an toàn dữ liệu
            points = roi_data.get("points", [])
            if not points or len(points) < 2: 
                continue
                
            target = roi_data.get("target", 0)
            current_count = roi_counts.get(i, 0)
            
            # 2. Xác định màu sắc (Alpha nên để 1.0 để rõ ràng)
            roi_color = (1.0, 0.0, 0.0, 1.0) if current_count != target else (0.0, 1.0, 0.0, 1.0)

            # 3. Lấy Display Meta (Mỗi vùng ROI 1 Meta riêng để tránh tràn giới hạn 16)
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta)
            
            # 4. Vẽ Lines (Cạnh của ROI)
            num_points = len(points)
            display_meta.num_lines = min(num_points, 16) # DeepStream giới hạn 16 lines/meta
            
            for j in range(display_meta.num_lines):
                line = display_meta.line_params[j]
                # BẮT BUỘC ép kiểu int cho tọa độ để tránh Segfault
                p1 = points[j]
                p2 = points[(j + 1) % num_points]
                line.x1 = int(p1[0])
                line.y1 = int(p1[1])
                line.x2 = int(p2[0])
                line.y2 = int(p2[1])
                line.line_width = 4
                line.line_color.set(*roi_color)

            # 5. Viết nhãn thông số (Label)
            display_meta.num_labels = 1
            txt = display_meta.text_params[0] 
            txt.display_text = f"ROI {i}: {current_count}/{target}"
            
            # Đảm bảo tọa độ chữ là số nguyên dương
            txt.x_offset = int(max(0, points[0][0]))
            txt.y_offset = int(max(0, points[0][1] - 25))
            
            txt.font_params.font_name = "Serif"
            txt.font_params.font_size = 12
            txt.font_params.font_color.set(1.0, 1.0, 1.0, 1.0)
            txt.set_bg_clr = 1
            txt.text_bg_clr.set(0.0, 0.0, 0.0, 0.7)

            # 6. Add Meta vào Frame
            pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)

    def _line_analytics_probe(self, frame_meta, batch_meta, gst_buffer): 
        source_id = frame_meta.source_id 
        self.frame_counters[source_id] = self.frame_counters.get(source_id, 0) + 1 
        actual_frame_num = self.frame_counters[source_id] 
        l_obj = frame_meta.obj_meta_list 
        
        while l_obj is not None: 
            try: 
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data) 
            except StopIteration: 
                break 
            
            obj_id = obj_meta.object_id 

            is_already_in = obj_id in self.crossed_objects_in 
            is_already_out = obj_id in self.crossed_objects_out 
            
            # 1. Mặc định ẩn box để sạch khung hình
            obj_meta.rect_params.border_width = 0
            # obj_meta.rect_params.border_color.set(0.0, 1.0, 0.0, 0.3)
            obj_meta.text_params.display_text = "" 
            
            l_user_meta = obj_meta.obj_user_meta_list 
            while l_user_meta is not None: 
                try: 
                    user_meta = pyds.NvDsUserMeta.cast(l_user_meta.data) 
                    if user_meta.base_meta.meta_type == pyds.NvDsMetaType.NVDS_OBJ_META_NVDSANALYTICS: 
                        analytics_meta = pyds.NvDsAnalyticsObjInfo.cast(user_meta.user_meta_data) 
                        
                        if analytics_meta.lcStatus: 
                            for status in analytics_meta.lcStatus:
                                # Xử lý EXIT
                                if "Exit" in status and not is_already_out: 
                                    if source_id not in self.stream_counts:
                                        self.stream_counts[source_id] = {'in': 0, 'out': 0}
                                    self.stream_counts[source_id]['out'] += 1
                                    self.crossed_objects_out.add(obj_id) # Quan trọng: Lưu ID lại
                                    is_already_out = True
                                    # Xóa khỏi danh sách ngược lại nếu cần (tùy logic nghiệp vụ)
                                    self.crossed_objects_in.discard(obj_id)
                                    is_already_in = False
                                    
                                    self.save_requests[source_id] = {"event": "exit", "frame_num": actual_frame_num, "obj": obj_meta}

                                # Xử lý ENTRY
                                elif "Entry" in status and not is_already_in: 
                                    if source_id not in self.stream_counts:
                                        self.stream_counts[source_id] = {'in': 0, 'out': 0}
                                    self.stream_counts[source_id]['in'] += 1
                                    self.crossed_objects_in.add(obj_id) # Quan trọng: Lưu ID lại
                                    is_already_in = True
                                    self.crossed_objects_out.discard(obj_id)
                                    is_already_out = False
                                    
                                    self.save_requests[source_id] = {"event": "entry", "frame_num": actual_frame_num, "obj": obj_meta}
                except StopIteration: 
                    break 
                l_user_meta = l_user_meta.next 
            
            # 2. VẼ BOX DỰA TRÊN TRẠNG THÁI ĐÃ CẬP NHẬT
            if is_already_out: 
                self._set_obj_display(obj_meta, "EXITED", (0.0, 0.0, 1.0, 1.0)) # Blue
            elif is_already_in: 
                self._set_obj_display(obj_meta, "ENTERED", (1.0, 0.0, 0.0, 1.0)) # Red
            
            try: 
                l_obj = l_obj.next 
            except StopIteration: 
                break 
                
        if self.frame_counters[source_id] % 100 == 0: 
            self._manage_id_cache()

    def analytics_src_pad_probe(self, pad, info, u_data): 
        gst_buffer = info.get_buffer() 
        if not gst_buffer: 
            return Gst.PadProbeReturn.OK 
            
        ptr_buffer = hash(gst_buffer) 
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(ptr_buffer) 
        l_frame = batch_meta.frame_meta_list 
        
        while l_frame: 
            try: 
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data) 
                source_id = frame_meta.source_id
                current_fps = self._get_fps(source_id) 
                if self.is_line_enabled:
                    self._line_analytics_probe(frame_meta, batch_meta, gst_buffer)
                    self._show_dashboard_line(frame_meta, batch_meta, current_fps)
                if self.is_roi_enabled:
                    roi_list = self.roi_points_cache.get(source_id, [])
                    if roi_list: 
                        # 1. Đếm tức thời (Raw count)
                        raw_roi_counts = self._roi_analytics_probe(frame_meta, len(roi_list))
                        
                        # 2. Làm mượt bằng Mode của 10 lần gần nhất
                        stable_roi_counts = self._get_stable_roi_counts(source_id, raw_roi_counts)
                        
                        # 3. Tính tổng từ số đã làm mượt để hiện Dashboard
                        total_in_roi = sum(stable_roi_counts.values())
                        
                        # 4. Vẽ (Dùng stable_roi_counts thay vì raw_roi_counts)
                        self._draw_roi_area(frame_meta, batch_meta, stable_roi_counts, roi_list)
                        self._show_dashboard_roi(frame_meta, batch_meta, current_fps, total_in_roi)
            except StopIteration: 
                break 
            except Exception as e:
                logger.error(f"Lỗi trong probe tại stream {source_id}: {e}")
                
            l_frame = l_frame.next 
            
        return Gst.PadProbeReturn.OK


    def _save_event_image(self, gst_buffer, obj_meta, frame_meta, event_type, actual_frame_num):
        if not os.path.exists(self.save_path): 
            os.makedirs(self.save_path, exist_ok=True)
            print("--- Đã tạo folder mới thành công ---")
        
        obj_enc_params = pyds.NvDsObjEncUsrArgs()
        obj_enc_params.saveImg = True
        obj_enc_params.isFrame = True 
        obj_enc_params.quality = 80
        
        filename = f"{self.cam_ids[0]}_f{actual_frame_num}_{event_type}_{int(datetime.now().timestamp())}.jpg"
        
        obj_enc_params.fileNameImg = os.path.join(self.save_path, filename)
        logger.info(f"save at {os.path.join(self.save_path, filename)}")

        pyds.nvds_obj_enc_process(self.obj_encoder, obj_enc_params, gst_buffer, obj_meta, frame_meta)
        
        self.save_queue.put(filename)

    def after_osd_pad_probe(self, pad, info, u_data):
        gst_buffer = info.get_buffer()
        if not gst_buffer:
            return Gst.PadProbeReturn.OK

        ptr_buffer = hash(gst_buffer)
        batch_meta = pyds.gst_buffer_get_nvds_batch_meta(ptr_buffer)
        l_frame = batch_meta.frame_meta_list

        while l_frame is not None:
            try:
                frame_meta = pyds.NvDsFrameMeta.cast(l_frame.data)
                source_id = frame_meta.source_id
                
                if source_id in self.save_requests:
                    req = self.save_requests.pop(source_id)
                    
                    self._save_event_image(
                        ptr_buffer, # Truyền pointer của buffer
                        req["obj"], 
                        frame_meta, 
                        req["event"], 
                        req["frame_num"]
                    )
            except StopIteration:
                break
            
            l_frame = l_frame.next

        return Gst.PadProbeReturn.OK
    
    def build(self, rtsp_path="/ds-test"): 
        logger.info("Building Pipeline Components...") 
        self.pipeline = Gst.Pipeline() 
        
        # 1. Streammux 
        self.streammux = self.create_element("nvstreammux", "Stream-muxer", ELEMENTS["streammux"]["properties"]) 
        self.streammux.set_property("batch-size", self.number_sources) 
        self.streammux.set_property("width", self.width_frame)
        self.streammux.set_property("height", self.height_frame)

        self.pipeline.add(self.streammux) 
        
        # 2. Source Bins 
        for i, uri in enumerate(self.uris): 
            logger.info(f"Adding source {i}: {uri}") 
            source_bin = self.create_source_bin(i, uri) 
            self.pipeline.add(source_bin) 
            sinkpad = self.streammux.request_pad_simple(f"sink_{i}") 
            source_bin.get_static_pad("src").link(sinkpad) 
            
        # 3. Downstream 
        order = ["pgie", "tracker", "nvdsanalytics", "tiler", "nvvidconv", "nvosd", "nvvidconv_postosd", "capsfilter", "encoder", "rtppay", "sink"] 
        prev = self.streammux 
        
        for name in order: 
            curr = self.create_element(ELEMENTS[name]["factory_name"], name, ELEMENTS[name]["properties"]) 

            if name == "tiler":
                curr.set_property("width", self.width_frame)
                curr.set_property("height", self.height_frame)
            
            if name == "nvdsanalytics":
                curr.set_property("config-file", self.final_cfg_path.as_posix())\
            
            if name == "sink":
                curr.set_property("port", self.udp_port) # Gán port UDP riêng
                curr.set_property("host", "127.0.0.1")

            self.pipeline.add(curr) 
            prev.link(curr) 
            prev = curr 
            
            if name == "nvdsanalytics": 
                curr.set_property("config-file", self.final_cfg_path.as_posix())
                osd_src_pad = curr.get_static_pad("src") 
                if osd_src_pad: 
                    osd_src_pad.add_probe(Gst.PadProbeType.BUFFER, self.analytics_src_pad_probe, 0) 
                    # osd_src_pad.add_probe(Gst.PadProbeType.BUFFER, self.after_osd_pad_probe, 0)
                    logger.info("--- Gắn Probe vào OSD SINK để Dashboard có thể hiển thị ---") 

            
            if name == "capsfilter" and self.is_line_enabled:
                caps_src_pad = curr.get_static_pad("src")
                if caps_src_pad:
                    caps_src_pad.add_probe(Gst.PadProbeType.BUFFER, self.after_osd_pad_probe, 0)
                    logger.info("--- Gắn Probe vào Capsfilter để chụp ảnh Full Frame có Box ---")
                    
        self.setup_rtsp(self.udp_port, self.rtsp_port, rtsp_path) 

    def setup_rtsp(self, udp_port, rtsp_port, path): 
        self.rtsp_server = GstRtspServer.RTSPServer.new() 
        self.rtsp_server.set_service(str(rtsp_port)) 
        
        factory = GstRtspServer.RTSPMediaFactory.new() 
        launch_cmd = ( 
            f'( udpsrc name=pay0 port={udp_port} buffer-size=524288 ' 
            f'caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, ' 
            f'encoding-name=(string)H264, payload=(int)96" )' 
        ) 
        factory.set_launch(launch_cmd) 
        factory.set_shared(True) 
        
        if not path.startswith("/"):
            path = "/" + path
            
        self.rtsp_server.get_mount_points().add_factory(path, factory) 
        self.rtsp_server.attach(None) 
        
        logger.info(f"RTSP Server live: rtsp://localhost:{rtsp_port}{path}")


    def run(self): 
        logger.info("Starting Pipeline...") 
        self.pipeline.set_state(Gst.State.PLAYING) 
        try: 
            self.loop.run() 
        except Exception as e: 
            logger.error(f"Runtime Error: {e}") 
        finally: 
            # 1. Dọn dẹp tài nguyên (luồng ngầm, bộ nhớ GPU) trước 
            self.stop() 
            # 2. Sau đó mới hạ pipeline về NULL 
            self.pipeline.set_state(Gst.State.NULL) 
            logger.info("Pipeline Stopped.") 

    def stop(self): 
        """Dọn dẹp luồng và giải phóng Context an toàn"""
        logger.info("Đang tiến hành dọn dẹp tài nguyên...")
        
        # Báo hiệu cho luồng ngầm tự đóng
        if hasattr(self, 'is_running'):
            self.is_running = False
            
            
        # Hủy context an toàn sau khi background process đã dừng
        if self.obj_encoder: 
            pyds.nvds_obj_enc_destroy_context(self.obj_encoder) 
            self.obj_encoder = None 
            logger.info("Đã giải phóng Object Encoder Context")

def start_pipeline():
    parser = argparse.ArgumentParser(description="DeepStream Analytics Pipeline")
    parser.add_argument("uris", nargs="*", help="Danh sách URIs camera")
    parser.add_argument("ids", nargs="*", help="Danh sách Ids camera")
    
    # Thêm các tham số cấu hình đầu vào
    parser.add_argument("--roi", type=str, help="Đường dẫn file txt chứa tọa độ ROI")
    parser.add_argument("--line", type=str, help="Đường dẫn file txt chứa tọa độ Line")

    parser.add_argument("--udpport", type=int, default=5400)
    parser.add_argument("--rtspport", type=int, default=9112)
    parser.add_argument("--pathrtsp", type=str, default="/ds-test", help="Path cho luồng RTSP Output")
    
    args = parser.parse_args()

    # 1. Lấy URIs từ biến môi trường CAM_URIS, nếu không có thì mới lấy từ sys.argv 
    env_uris = os.environ.get("CAM_URIS") 
    if env_uris: 
        uris = [u.strip() for u in env_uris.split(",") if u.strip()] 
        logger.info(f"Loaded {len(uris)} URIs from environment variables.") 
    else: 
        uris = sys.argv[1:] 
        
    # 2. Lấy CAM_IDS tương ứng (để map ID vào log/output) 
    env_ids = os.environ.get("CAM_IDS") 
    cam_ids = [c.strip() for c in env_ids.split(",") if c.strip()] if env_ids else [] 
    
    # Kiểm tra nếu cuối cùng vẫn không có URI nào 
    if not uris: 
        logger.error("No URIs found! Please set CAM_URIS or pass as arguments.") 
        sys.exit(1) 
        
    # Khởi tạo và chạy 
    pipe = Pipeline(uris,roi_input=args.roi, line_input=args.line, udp_port=args.udpport,rtsp_port=args.rtspport) 
    pipe.cam_ids = cam_ids 
    pipe.build(rtsp_path=args.pathrtsp) 
    pipe.run()
if __name__ == "__main__":
    start_pipeline()