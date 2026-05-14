import os
import logging

logger = logging.getLogger(__name__)

class LineConfigHandler:
    def __init__(self):
        self.line_streams = []

    def read_config(self, file_path):
        all_line_data = []
        try:
            if not file_path or not os.path.exists(file_path):
                return []
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                segments = [seg.strip() for seg in content.split('---') if seg.strip()]
                for seg in segments:
                    lines = [l.strip() for l in seg.split('\n') if l.strip() and not l.startswith('#')]
                    if len(lines) >= 2:
                        all_line_data.append({"entry": lines[0], "exit": lines[1]})
        except Exception as e:
            logger.error(f"Lỗi khi đọc file Line: {e}")
        
        self.line_streams = all_line_data
        return all_line_data

    def get_config_content(self):
        content = ""
        for i, data in enumerate(self.line_streams):
            content += f"[line-crossing-stream-{i}]\nenable=1\n"
            content += f"line-crossing-Entry={data['entry']}\n"
            content += f"line-crossing-Exit={data['exit']}\n"
            content += f"class-id=0\nextended=0\nmode=loose\n\n"
        return content
