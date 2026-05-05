import os
from dotenv import load_dotenv
from pathlib import Path
load_dotenv()

MODEL_DIR = os.environ.get( "MODEL_DIR", (Path(__file__).parents[1] / "models").as_posix())
CONFIG_DIR = os.environ.get( "CONFIG_DIR", (Path(__file__).parents[1] / "config").as_posix())
CONFIG_INPUT = os.environ.get( "CONFIG_INPUT", (Path(__file__).parents[1] / "root_config").as_posix())