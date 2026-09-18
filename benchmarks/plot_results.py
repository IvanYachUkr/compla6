"""Render whole-corpus tables and Pareto plots; accepts other benchmark inputs too."""
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / 'src'))
from compression_lab.reporting import main


if __name__ == '__main__':
    raise SystemExit(main(default_inputs=[HERE / 'recorded/whole/trials.json'],
                          default_out=HERE / '_runs/whole-report', plots=True))
