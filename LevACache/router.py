"""확장자 → 모델 라우팅. pdf/이미지는 비전(MiniCPM-V), 그 외는 텍스트(Gemma)."""
import os

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff"}
PDF_EXTS = {".pdf"}
# 텍스트로 추출 가능한 문서 + 소스코드
TEXT_EXTS = {".txt", ".md", ".log", ".csv", ".json", ".xml", ".html", ".htm",
             ".docx", ".xlsx", ".pptx", ".hwp", ".rtf",
             ".py", ".js", ".ts", ".jsx", ".tsx", ".vue",
             ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".cs",
             ".java", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
             ".sh", ".bat", ".ps1", ".sql", ".yaml", ".yml", ".toml", ".ini",
             ".lua", ".pl", ".r"}

VISION = "vision"   # MiniCPM-V
TEXT = "text"       # Gemma


# 항상 무시하는 시스템/임시 파일(하드코딩 차단)
BLOCKED_NAMES = {"desktop.ini", "thumbs.db", "ehthumbs.db", ".ds_store",
                 "ntuser.dat", "iconcache.db"}


def is_blocked(path):
    name = os.path.basename(path).lower()
    return name in BLOCKED_NAMES or name.startswith("~$")


def route(path):
    """파일 경로 → (model_key, ext). 지원 안 하거나 차단 대상이면 (None, ext)."""
    ext = os.path.splitext(path)[1].lower()
    if is_blocked(path):
        return None, ext
    if ext in PDF_EXTS or ext in IMAGE_EXTS:
        return VISION, ext
    if ext in TEXT_EXTS:
        return TEXT, ext
    return None, ext


def is_supported(path):
    key, _ = route(path)
    return key is not None
