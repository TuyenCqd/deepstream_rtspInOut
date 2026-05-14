import os
import logging

logger = logging.getLogger(__name__)

class ROIConfigHandler:
    def __init__(self):
        self.roi_streams = []

    def read_config(self, file_path):
        all_cameras_data = []
        try:
            if not file_path or not os.path.exists(file_path):
                return []
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            
            camera_segments = content.split('===')
            for cam_seg in camera_segments:
                cam_seg = cam_seg.strip()
                if not cam_seg: continue
                
                roi_segments = cam_seg.split('---')
                current_cam_rois = []
                for roi_seg in roi_segments:
                    lines = [l.strip() for l in roi_seg.split('\n') if l.strip() and not l.startswith('#')]
                    if not lines: continue
                    try:
                        target = int(lines[0])
                        points = [(int(x.split(',')[0]), int(x.split(',')[1])) for x in lines[1:] if ',' in x]
                        current_cam_rois.append({"target": target, "points": points})
                    except Exception as e:
                        logger.error(f"Lỗi phân tích ROI: {e}")
                if current_cam_rois:
                    all_cameras_data.append(current_cam_rois)
        except Exception as e:
            logger.error(f"Lỗi đọc file ROI: {e}")
        
        self.roi_streams = all_cameras_data
        return all_cameras_data

    def get_config_content(self):
        content = ""
        for i, rois in enumerate(self.roi_streams):
            content += f"[roi-filtering-stream-{i}]\nenable=1\n"
            for j, roi_data in enumerate(rois):
                flattened = []
                for p in roi_data["points"]:
                    flattened.extend([str(p[0]), str(p[1])])
                roi_str = ";".join(flattened) + ";"
                content += f"roi-C{j:02d}={roi_str}\n"
            content += "inverse-roi=0\nclass-id=0\n\n"
        return content
