# main.py
import argparse
import os
from pathlib import Path
import sys
import time
import math
import queue
import threading
from collections import OrderedDict

# from grpc import server

from config.const import CONFIG_DIR, MODEL_DIR
import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstRtspServer", "1.0")
from gi.repository import Gst, GstRtspServer, GLib
import pyds
from loguru import logger
import time
from collections import deque, Counter
import logging

# logger = logging.getLogger(__name__)
logger.remove()
logger.add(sys.stdout, colorize=True, format="<green>{time:HH:mm:ss}</green> | <level>{message}</level>", level="INFO")

logger.info("Test log: Loguru đã sẵn sàng!")

from config.pipeline import ELEMENTS
from processor.deepstream_base import BasePipeline

from utils.resolution_video import get_optimal_streammux_resolution

class Pipeline(BasePipeline):
    __instance = None

    def __new__(cls, *args, **kwargs):
        if cls.__instance is None:
            cls.__instance = super(Pipeline, cls).__new__(cls)
            cls.__instance.__initialized = False
        return cls.__instance

    def __init__(self, uris, roi_input=None, line_input=None, width=1920, height=1080):
        if self.__initialized:
            return
        super().__init__(uris=uris) 
        
        self.uris = uris
        self.number_sources = len(uris)
        self.loop = GLib.MainLoop()

        self.width_frame = width
        self.height_frame = height

        # Biến theo dõi Analytics
        self.persons_previously_in_roi = set()  
        self.crossed_objects_in = set()        
        self.crossed_objects_out = set() 
        self.fps_data = {}
        self.crossed_objects_in = OrderedDict()
        self.crossed_objects_out = OrderedDict()
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

        self.final_cfg_path = Path(CONFIG_DIR) / "config_nvdsanalytics_final.txt" 
        self.cfg_content = ( f"[property]\nenable=1\nconfig-width={self.width_frame}\nconfig-height={self.height_frame}\n" "osd-mode=1\ndisplay-font-size=12\n\n" ) 
        
        self.config_model_path = Path(MODEL_DIR) / "traffic_rtdetr/config_infer_primary_rtdetr.txt"

        if roi_input: 
            self.is_roi_enabled = True 
            self.update_person_confidence(self.config_model_path, self.is_roi_enabled)
            
            self.roi_streams = self.read_multi_stream_roi(roi_input) 
            self._append_roi_config_content() 
            self.roi_points_cache = {i: stream_data for i, stream_data in enumerate(self.roi_streams)} 
        
        if line_input: 
            self.is_line_enabled = True 
            self.update_person_confidence(self.config_model_path, self.is_roi_enabled)
            self.line_streams = self.read_multi_stream_line(line_input) 
            self._append_line_config_content() 

        # 3. Sau khi build xong nội dung mới lưu file Analytics
        self._save_final_config() 
            
        self.__initialized = True 
        logger.info("Pipeline Initialized Successfully!")

    def update_person_confidence(self, config_model_path, is_roi_mode): 
        # Xác định ngưỡng confidence dựa trên tham số đầu vào 
        new_conf = 0.6 if is_roi_mode else 0.25 
        if not os.path.exists(config_model_path): 
            print(f"Lỗi: Không tìm thấy file config tại {config_model_path}") 
            return 
        with open(config_model_path, 'r') as f: 
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
            with open(config_model_path, 'w') as f: 
                f.writelines(new_lines) 
            print(f"--- Đã cập nhật Person Confidence: {new_conf} (Mode: {'ROI' if is_roi_mode else 'LINE'}) ---")

    def _append_roi_config_content(self): 
        # Duyệt qua từng camera (stream) 
        for i, rois in enumerate(self.roi_streams): 
            self.cfg_content += f"[roi-filtering-stream-{i}]\nenable=1\n" 
            # Duyệt qua từng vùng ROI trong camera đó 
            for j, roi_data in enumerate(rois): 
                flattened = [] 
                for p in roi_data["points"]: 
                    flattened.extend([str(p[0]), str(p[1])]) 
                roi_str = ";".join(flattened) + ";" 
                # Ghi mỗi vùng một dòng riêng biệt: roi-C0, roi-C1... 
                self.cfg_content += f"roi-C{j:02d}={roi_str}\n" 
            self.cfg_content += "inverse-roi=0\nclass-id=0\n\n" 

    def _append_line_config_content(self): 
        """Tạo chuỗi config cho Line từ dữ liệu đã đọc""" 
        for i, data in enumerate(self.line_streams): 
            # data giả định là {"entry": "...", "exit": "..."} 
            self.cfg_content += f"[line-crossing-stream-{i}]\nenable=1\n" 
            self.cfg_content += f"line-crossing-Entry={data['entry']}\n" 
            self.cfg_content += f"line-crossing-Exit={data['exit']}\n" 
            self.cfg_content += f"class-id=0\nextended=0\nmode=loose\n\n" 

    def _save_final_config(self): 
        try:
            # 1. Tạo thư mục
            print("DEBUG: Preparing directory...")
            self.final_cfg_path.parent.mkdir(parents=True, exist_ok=True)
            
            # 2. Ghi file
            print(f"--- Saving final config to {self.final_cfg_path} ---")
            with open(self.final_cfg_path, "w", encoding="utf-8") as f: 
                f.write(self.cfg_content) 
                f.flush() # Đẩy dữ liệu từ buffer xuống đĩa ngay lập tức
                os.fsync(f.fileno()) # Đảm bảo file được ghi xong
            print("DEBUG: File write completed.")

            # 3. Cấp quyền (Thường là nơi gây lỗi trong Docker)
            print("DEBUG: Changing file permissions...")
            try:
                # Kiểm tra xem hệ điều hành có hỗ trợ không trước khi chạy
                os.chmod(str(self.final_cfg_path), 0o666) 
            except Exception as e:
                print(f"DEBUG: Warning - Could not change permissions: {e}")
            
            print(f"SUCCESS: Final config generated at {self.final_cfg_path}")
            logger.info(f" Final config generated at {self.final_cfg_path}") 

        except Exception as e:
            print(f"CRITICAL ERROR in _save_final_config: {e}")
            logger.error(f"Error: {e}")


    def read_multi_stream_roi(self, file_path): 
        all_cameras_data = [] 
        try: 
            with open(file_path, 'r', encoding='utf-8') as f: 
                content = f.read() 
            # Bước 1: Tách các Camera bằng "===" 
            camera_segments = content.split('===') 
            for cam_seg in camera_segments: 
                cam_seg = cam_seg.strip() 
                if not cam_seg: 
                    continue 
            # Bước 2: Tách các ROI trong mỗi Camera bằng "---" 
            roi_segments = cam_seg.split('---') 
            current_cam_rois = [] 
            for roi_seg in roi_segments: 
                lines = [l.strip() for l in roi_seg.split('\n') if l.strip() and not l.startswith('#')] 
                if not lines: 
                    continue 
                try: 
                    target = int(lines[0]) 
                    points = [] 
                    for p_line in lines[1:]: 
                        coords = [int(x.strip()) for x in p_line.split(',')] 
                        if len(coords) == 2: 
                            points.append((coords[0], coords[1])) 
                    current_cam_rois.append({"target": target, "points": points}) 
                except Exception as e: 
                    logger.error(f"Lỗi phân tích ROI: {e}") 
            if current_cam_rois: 
                all_cameras_data.append(current_cam_rois) 
        except Exception as e: 
            logger.error(f"Lỗi đọc file: {e}") 
        logger.info(f"Đã đọc cấu hình ROI cho {len(all_cameras_data)} camera từ file {file_path}")
        return all_cameras_data 

    def read_multi_stream_line(self, file_path): 
        all_line_data = [] 
        try: 
            if not file_path or not os.path.exists(file_path): 
                logger.warning(f"File line không tồn tại hoặc không được cung cấp: {file_path}") 
                return [] 
            with open(file_path, 'r', encoding='utf-8') as f: 
                content = f.read() 
            # Tách nội dung theo từng camera bằng dấu --- 
            segments = [seg.strip() for seg in content.split('---') if seg.strip()] 
            for seg in segments: 
                # Lọc bỏ các dòng comment (#) và dòng trống 
                lines = [l.strip() for l in seg.split('\n') if l.strip() and not l.startswith('#')] 
                if len(lines) >= 2: 
                    all_line_data.append({ "entry": lines[0], "exit": lines[1] }) 
                else: 
                    logger.warning(f"Một đoạn trong file line bị thiếu dữ liệu (cần ít nhất 2 dòng Entry/Exit)") 
                    all_line_data.append({"entry": "", "exit": ""}) 
        except Exception as e: 
            logger.error(f"Lỗi khi đọc file cấu hình Line: {e}") 
        return all_line_data 

    def _manage_id_cache(self):
        # Duyệt qua các tập hợp lưu trữ ID để dọn dẹp
        for cache_set in [self.crossed_objects_in, self.crossed_objects_out, self.persons_previously_in_roi]:
            if len(cache_set) > self.max_cache_size:
                # Chuyển thành list để lấy các phần tử cũ nhất (đầu danh sách)
                # Lấy 100 phần tử để xóa bớt mỗi lần dọn dẹp
                items_to_remove = list(cache_set)[:100]
                for item in items_to_remove:
                    cache_set.discard(item)
        # Ghi log nếu cần theo dõi
        # logger.debug("ID Cache cleaned up.")


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
        if source_id not in self.roi_history: 
            self.roi_history[source_id] = {} 
        stable_counts = {} 
        for roi_idx, count in current_roi_counts.items(): 
            if roi_idx not in self.roi_history[source_id]: 
                self.roi_history[source_id][roi_idx] = deque(maxlen=20) 
            # Thêm giá trị mới vào hàng đợi 
            self.roi_history[source_id][roi_idx].append(count) 
            # Lấy giá trị xuất hiện nhiều nhất (Mode) 
            counts_in_history = self.roi_history[source_id][roi_idx] 
            most_common = Counter(counts_in_history).most_common(1)[0][0] 
            stable_counts[roi_idx] = most_common 
        return stable_counts 

    def _set_obj_display(self, obj_meta, text, color, bg_opacity=0.2): 
        # 1. Cấu hình Viền (Border) 
        obj_meta.rect_params.border_width = 3 
        obj_meta.rect_params.border_color.set(*color) 
        # 2. Cấu hình Màu nền khối (Background Rect) - obj_meta.rect_params.has_bg_color = 1 
        obj_meta.rect_params.bg_color.set(color[0], color[1], color[2], bg_opacity) 
        # 3. Cấu hình Chữ (Text) 
        obj_meta.text_params.display_text = text 
        obj_meta.text_params.font_params.font_size = 10 
        obj_meta.text_params.font_params.font_color.set(1.0, 1.0, 1.0, 1.0) 
        # Chữ trắng 
        # 4. Cấu hình Nền của chữ (Text Background) 
        obj_meta.text_params.set_bg_clr = 1 
        obj_meta.text_params.text_bg_clr.set(color[0], color[1], color[2], 0.6) 

    def _show_dashboard_roi(self, frame_meta, batch_meta, fps_val, num_in_roi): 
        source_id = frame_meta.source_id 
        # 1. Lấy danh sách các ROI (trả về List) 
        roi_list = self.roi_points_cache.get(source_id, []) 
        # 2. Tính tổng target của tất cả các vùng ROI trong stream này 
        # Nếu không có vùng nào thì target mặc định là 0 
        total_target = sum(roi.get("target", 0) for roi in roi_list) if roi_list else 0 

        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta) 
        display_meta.num_labels = 1 
        txt = display_meta.text_params[0] 
        # Hiển thị tổng số người trong các ROI / Tổng target của các ROI 
        txt.display_text = ( 
            f"CAM: {source_id} | FPS: {fps_val:.1f} | " 
            f"ROI TOTAL: {num_in_roi}/{total_target}" 
        ) 
        # Vị trí hiển thị (Góc trên bên trái) 
        txt.x_offset, txt.y_offset = 30, 30 
        # Cấu hình Font và Màu sắc 
        txt.font_params.font_name = "Serif" 
        txt.font_params.font_size = 14 
        txt.font_params.font_color.set(1.0, 1.0, 1.0, 1.0) 
        # Màu nền cho chữ (Đen mờ) 
        txt.set_bg_clr = 1 
        txt.text_bg_clr.set(0.0, 0.0, 0.0, 0.7) 
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta) 

    def _show_dashboard_line(self, frame_meta, batch_meta, fps_val): 
        # 1. Lấy dữ liệu đếm từ stream_counts
        source_id = frame_meta.source_id 
        counts = self.stream_counts.get(source_id, {'in': 0, 'out': 0}) 
        total_in = counts['in'] 
        total_out = counts['out'] 

        # 2. Lấy display_meta từ pool
        display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta) 
        display_meta.num_labels = 1 
        txt = display_meta.text_params[0] 

        # 3. Cấu hình nội dung hiển thị
        # Sử dụng ký tự mũi tên hoặc màu sắc để phân biệt IN/OUT cho oách
        txt.display_text = f"CAM: {source_id} | FPS: {fps_val:.1f} | IN: {total_in} | OUT: {total_out}" 

        # 4. Cấu hình vị trí và font
        txt.x_offset = 30
        txt.y_offset = 30 
        txt.font_params.font_name = "Serif" 
        txt.font_params.font_size = 14 
        txt.font_params.font_color.set(1.0, 1.0, 1.0, 1.0) # Màu chữ trắng
        
        # 5. Cấu hình nền cho chữ (Background)
        txt.set_bg_clr = 1 
        # Màu nền đen mờ (RGBA: 0,0,0,0.7) giúp chữ nổi bật trên mọi nền video
        txt.text_bg_clr.set(0.0, 0.0, 0.0, 0.7) 
        
        # 6. Thêm meta vào frame
        pyds.nvds_add_display_meta_to_frame(frame_meta, display_meta)


    def _roi_analytics_probe(self, frame_meta, num_rois): 
        # 1. Khởi tạo 
        roi_counts = {i: 0 for i in range(num_rois)} 
        l_obj = frame_meta.obj_meta_list 
        
        while l_obj: 
            try: 
                obj_meta = pyds.NvDsObjectMeta.cast(l_obj.data) 
            except Exception as e: 
                print(f"Error casting object metadata: {e}") 
                l_obj = l_obj.next 
                continue 
                
            obj_id = obj_meta.object_id 
            
            # Chỉ xử lý class Người (Class ID 0)
            if obj_meta.class_id == 0: 
                # Reset hiển thị mặc định cho từng object người
                obj_meta.rect_params.border_width = 0 
                obj_meta.text_params.display_text = "" 
                
                is_now_in_any_roi = False 
                l_user_meta = obj_meta.obj_user_meta_list 
                
                while l_user_meta: 
                    user_meta = pyds.NvDsUserMeta.cast(l_user_meta.data) 
                    if user_meta.base_meta.meta_type == pyds.nvds_get_user_meta_type("NVIDIA.DSANALYTICSOBJ.USER_META"): 
                        analytics_meta = pyds.NvDsAnalyticsObjInfo.cast(user_meta.user_meta_data) 
                        
                        # KIỂM TRA VÀ ĐẾM TỪNG VÙNG 
                        if analytics_meta.roiStatus: 
                            is_now_in_any_roi = True 
                            for roi_id in analytics_meta.roiStatus: 
                                try: 
                                    # Lấy index từ tên "roi-C00", "roi-C01"...
                                    idx = int(roi_id.split('C')[-1]) 
                                    if idx in roi_counts: 
                                        roi_counts[idx] += 1 
                                except: 
                                    continue 
                    l_user_meta = l_user_meta.next # Phải nằm ngoài block if meta_type

                # Xử lý trạng thái hiển thị (Sau khi đã quét hết user_meta của object đó)
                if is_now_in_any_roi: 
                    self.persons_previously_in_roi.add(obj_id) 
                    self._set_obj_display(obj_meta, "IN ROI", (1.0, 0.0, 0.0, 1.0)) 
                elif obj_id in self.persons_previously_in_roi: 
                    self._set_obj_display(obj_meta, "EXITED", (0.0, 0.0, 1.0, 1.0)) 
                    self.persons_previously_in_roi.discard(obj_id) # Dùng discard để tránh lỗi nếu ID không tồn tại

            try:
                l_obj = l_obj.next 
            except StopIteration:
                break
                
        return roi_counts # Trả về sau khi đã duyệt HẾT danh sách object

    
    def _draw_roi_area(self, frame_meta, batch_meta, roi_counts, roi_list): 
        for i, roi_data in enumerate(roi_list): 
            # 1. Kiểm tra an toàn dữ liệu 
            points = roi_data.get("points", []) 
            if not points or len(points) < 2: continue 
            target = roi_data.get("target", 0) 
            current_count = roi_counts.get(i, 0) 
            # 2. Xác định màu sắc (Alpha nên để 1.0 để rõ ràng) 
            roi_color = (1.0, 0.0, 0.0, 1.0) if current_count != target else (0.0, 1.0, 0.0, 1.0) 
            # 3. Lấy Display Meta (Mỗi vùng ROI 1 Meta riêng để tránh tràn giới hạn 16) 
            display_meta = pyds.nvds_acquire_display_meta_from_pool(batch_meta) 
            # 4. Vẽ Lines (Cạnh của ROI) 
            num_points = len(points) 
            display_meta.num_lines = min(num_points, 16) 
            # DeepStream giới hạn 16 lines/meta 
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

            # 1. Mặc định ẩn box
            obj_meta.rect_params.border_width = 0 
            obj_meta.text_params.display_text = "" 
            
            l_user_meta = obj_meta.obj_user_meta_list 
            while l_user_meta is not None: 
                user_meta = pyds.NvDsUserMeta.cast(l_user_meta.data) 
                # Sửa lỗi tên thuộc tính: baseMeta -> base_meta, metaType -> meta_type
                if user_meta.base_meta.meta_type == pyds.nvds_get_user_meta_type("NVIDIA.DSANALYTICSOBJ.USER_META"): 
                    # Sửa lỗi: userMetaData -> user_meta_data
                    analytics_meta = pyds.NvDsAnalyticsObjInfo.cast(user_meta.user_meta_data) 
                    
                    if analytics_meta.lcStatus: 
                        for status in analytics_meta.lcStatus: 
                            # Xử lý EXIT 
                            if "Exit" in status and not is_already_out: 
                                if source_id not in self.stream_counts: 
                                    self.stream_counts[source_id] = {'in': 0, 'out': 0} 
                                self.stream_counts[source_id]['out'] += 1 
                                self.crossed_objects_out.add(obj_id) 
                                is_already_out = True 
                                self.crossed_objects_in.discard(obj_id) 
                                is_already_in = False 
                                self.save_requests[source_id] = {"event": "exit", "frame_num": actual_frame_num, "obj": obj_meta} 
                            
                            # Xử lý ENTRY 
                            elif "Entry" in status and not is_already_in: 
                                if source_id not in self.stream_counts: 
                                    self.stream_counts[source_id] = {'in': 0, 'out': 0} 
                                self.stream_counts[source_id]['in'] += 1 
                                self.crossed_objects_in.add(obj_id) 
                                is_already_in = True 
                                self.crossed_objects_out.discard(obj_id) 
                                is_already_out = False 
                                self.save_requests[source_id] = {"event": "entry", "frame_num": actual_frame_num, "obj": obj_meta} 
                
                # CỰC KỲ QUAN TRỌNG: Phải đưa lệnh next ra ngoài block IF
                l_user_meta = l_user_meta.next 

            # 2. VẼ BOX DỰA TRÊN TRẠNG THÁI ĐÃ CẬP NHẬT 
            if is_already_out: 
                self._set_obj_display(obj_meta, "EXITED", (0.0, 0.0, 1.0, 1.0)) # Blue
            elif is_already_in: 
                self._set_obj_display(obj_meta, "ENTERED", (1.0, 0.0, 0.0, 1.0)) # Red 

            # Di chuyển tới Object tiếp theo
            try: 
                l_obj = l_obj.next 
            except StopIteration: 
                break 

        # Dọn dẹp cache định kỳ theo từng camera
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
        obj_enc_params = pyds.NvDsObjEncUsrArgs() 
        obj_enc_params.saveImg = True 
        obj_enc_params.isFrame = True 
        obj_enc_params.quality = 80 
        filename = f"cam{frame_meta.source_id}_f{actual_frame_num}_{event_type}.jpg" 
        logger.info(f"save at {filename}") 
        obj_enc_params.fileNameImg = os.path.join(self.save_path, filename) 
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
                        ptr_buffer,  # Truyền pointer của buffer 
                        req["obj"], 
                        frame_meta, 
                        req["event"], 
                        req["frame_num"] 
                    ) 
            except StopIteration: 
                break 
            l_frame = l_frame.next 
        return Gst.PadProbeReturn.OK
    
    def build(self): 
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

        # 3. Downstream (Sửa logic link ở đây)
        order = ["pgie", "tracker", "nvdsanalytics", "tiler", "nvvidconv", "nvosd", "nvvidconv_postosd", "capsfilter", "encoder", "rtppay", "sink"] 
        prev = self.streammux 

        for name in order: 
            curr = self.create_element(ELEMENTS[name]["factory_name"], name, ELEMENTS[name]["properties"]) 
            if not curr: continue # Bảo vệ nếu không tạo được element

            # Thêm vào pipeline và link ngay lập tức để không bị đứt đoạn
            self.pipeline.add(curr)
            prev.link(curr)

            # Cấu hình đặc biệt cho từng element
            if name == "tiler": 
                curr.set_property("width", self.width_frame) 
                curr.set_property("height", self.height_frame) 

            elif name == "nvdsanalytics": 
                curr.set_property("config-file", self.final_cfg_path.as_posix()) 
                # Gắn probe tính FPS và lấy data analytics
                ana_src_pad = curr.get_static_pad("src") 
                if ana_src_pad:
                    ana_src_pad.add_probe(Gst.PadProbeType.BUFFER, self.analytics_src_pad_probe, 0) 
                    logger.info("--- Đã gắn Probe Analytics & FPS ---") 

            elif name == "capsfilter":
                # Luôn đảm bảo caps filter được gán đúng định dạng từ ELEMENTS
                if self.is_line_enabled: 
                    caps_src_pad = curr.get_static_pad("src") 
                    if caps_src_pad: 
                        caps_src_pad.add_probe(Gst.PadProbeType.BUFFER, self.after_osd_pad_probe, 0) 
                        logger.info("--- Đã gắn Probe Capture (ROI Enabled) ---") 

            # Cập nhật prev để link phần tử kế tiếp
            prev = curr 

        self.setup_rtsp(5400, 9112, "/ds-test")
        logger.info("Pipeline Build Successful!")


    # def setup_rtsp(self, udp_port, rtsp_port, path): 
    #     server = GstRtspServer.RTSPServer.new() 
    #     server.set_service(str(rtsp_port)) 
    #     factory = GstRtspServer.RTSPMediaFactory.new() 
    #     launch_cmd = ( 
    #         f'( udpsrc name=pay0 port={udp_port} buffer-size=524288 ' 
    #         f'caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, ' 
    #         f'encoding-name=(string)H264, payload=(int)96" )' 
    #     ) 
    #     factory.set_launch(launch_cmd) 
    #     factory.set_shared(True) 
    #     server.get_mount_points().add_factory(path, factory) 
    #     server.attach(None) 
    #     logger.info(f"RTSP Server live: rtsp://localhost:{rtsp_port}{path}") 
    
    def setup_rtsp(self, udp_port, rtsp_port, path):
        # Gán vào self để server KHÔNG bị Python xóa
        self.rtsp_server = GstRtspServer.RTSPServer.new()
        self.rtsp_server.set_service(str(rtsp_port))
        
        factory = GstRtspServer.RTSPMediaFactory.new()
        # Thêm -rtsp-tcp để VLC ổn định hơn qua Docker
        launch_cmd = (
            f'( udpsrc name=pay0 port={udp_port} buffer-size=524288 '
            f'caps="application/x-rtp, media=(string)video, clock-rate=(int)90000, '
            f'encoding-name=(string)H264, payload=(int)96" )'
        )
        factory.set_launch(launch_cmd)
        factory.set_shared(True)
        
        self.rtsp_server.get_mount_points().add_factory(path, factory)
        self.rtsp_server.attach(None)
        
        # Lấy IP thật để log cho chuẩn
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try: s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close()
        except: ip = "127.0.0.1"
        
        logger.info(f"RTSP Server live: rtsp://{ip}:{rtsp_port}{path}")


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
    # def run(self):
    #     logger.info("Starting Pipeline...")
        
    #     # 1. Kiểm tra trạng thái SET_STATE
    #     ret = self.pipeline.set_state(Gst.State.PLAYING)
    #     if ret == Gst.StateChangeReturn.FAILURE:
    #         logger.error("ERROR: Không thể chuyển Pipeline sang trạng thái PLAYING. Hãy kiểm tra lại nguồn RTSP hoặc Config!")
    #         return
    #     elif ret == Gst.StateChangeReturn.ASYNC:
    #         logger.info("Pipeline đang ở trạng thái ASYNC (đang chờ kết nối nguồn)...")
        
    #     # 2. Debug cấu trúc Pipeline (Cực kỳ quan trọng)
    #     # Dòng này sẽ liệt kê tất cả các element đang có để xem có 'capsfilter' không
    #     logger.debug("--- Danh sách các Elements hiện có trong Pipeline ---")
    #     it = self.pipeline.iterate_elements()
    #     while True:
    #         res, elem = it.next()
    #         if res != Gst.IteratorResult.OK: break
    #         # In ra để so sánh tên chính xác
    #         logger.debug(f"Element found: {elem.get_name()}") 
        
    #     try:
    #         # Kiểm tra loop trước khi chạy
    #         if self.loop.is_running():
    #             logger.warning("MainLoop đã đang chạy từ trước đó.")
    #         else:
    #             self.loop.run()
    #     except Exception as e:
    #         logger.error(f"Runtime Error trong MainLoop: {e}")
    #     except KeyboardInterrupt:
    #         logger.info("Dừng pipeline bằng phím tắt (Ctrl+C).")
    #     finally:
    #         logger.info("Đang thực hiện dọn dẹp (Cleanup)...")
    #         self.stop()
    #         self.pipeline.set_state(Gst.State.NULL)
    #         logger.info("Pipeline đã dừng hẳn.")


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
    parser.add_argument("--roi", type=str, help="Đường dẫn file txt chứa tọa độ ROI") 
    parser.add_argument("--line", type=str, help="Đường dẫn file txt chứa tọa độ Line") 
    args = parser.parse_args() 

    # 1. Lấy URIs (Ưu tiên Environment Variable cho Docker)
    env_uris = os.environ.get("CAM_URIS") 
    if env_uris: 
        uris = [u.strip() for u in env_uris.split(",") if u.strip()] 
        logger.info(f"Loaded {len(uris)} URIs from environment variables.") 
    else: 
        uris = args.uris # Lấy từ parser thay vì sys.argv trực tiếp cho sạch

    # 2. Lấy CAM_IDS (Fix lỗi cú pháp dòng cam_ids)
    env_ids = os.environ.get("CAM_IDS") 
    if env_ids:
        cam_ids = [c.strip() for c in env_ids.split(",") if c.strip()]
    else:
        cam_ids = [f"cam_{i}" for i in range(len(uris))] # Tạo ID mặc định nếu trống

    # Kiểm tra URIs
    if not uris: 
        logger.error("No URIs found! Please set CAM_URIS or pass as arguments.") 
        sys.exit(1) 

    # 3. TỰ ĐỘNG DÒ ĐỘ PHÂN GIẢI (Cực kỳ quan trọng để tránh méo hình)
    width, height = get_optimal_streammux_resolution(uris)
    logger.info(f"Phát hiện độ phân giải tối ưu: {width}x{height}")

    # 4. Khởi tạo và chạy 
    # Truyền thêm thông số resolution vào class Pipeline
    pipe = Pipeline(uris, roi_input=args.roi, line_input=args.line, width=width, height=height) 
    pipe.cam_ids = cam_ids 
    
    pipe.build() 
    pipe.run()


if __name__ == "__main__": 
    start_pipeline()
