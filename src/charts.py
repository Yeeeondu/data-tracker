"""matplotlib 차트 공통 스타일 (한글 폰트 포함)."""
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402

for fam in ["NanumGothic", "Noto Sans CJK KR", "Noto Sans CJK JP", "Noto Sans CJK SC"]:
    if any(fam in f.name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = fam
        break
plt.rcParams.update({
    "axes.unicode_minus": False,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.color": "#e5e7eb",
    "grid.linewidth": 0.8,
    "axes.edgecolor": "#9ca3af",
    "axes.labelcolor": "#374151",
    "xtick.color": "#4b5563",
    "ytick.color": "#4b5563",
    "figure.dpi": 150,
})

ACCENT = "#2563eb"
MUTED = "#93c5fd"
