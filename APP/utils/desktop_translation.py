"""Desktop-only preferences. Plugin payloads never pass through these defaults."""
from __future__ import annotations

import copy
import json
from pathlib import Path

DEFAULTS = {
    "sourceLang": "en", "targetLang": "zh-CN", "engine": "pdf2zh_next",
    "service": "bing", "next_service": "siliconflowfree", "skipLastPages": 0,
    "threadNum": 4, "qps": 10, "poolSize": 0, "rename": True, "showProgress": True,
    "skipSubsetFonts": False, "babeldoc": False, "fontFile": "", "fontFamily": "auto",
    "dualMode": "LR", "transFirst": False, "ocr": False, "autoOcr": True,
    "noWatermark": True, "saveGlossary": False, "disableGlossary": False,
    "noDual": False, "noMono": False, "skipClean": False,
    "disableRichTextTranslate": False, "enhanceCompatibility": False,
    "translateTableText": False, "onlyIncludeTranslatedPage": False,
    "mono": True, "dual": True, "mono_cut": False, "dual_cut": False,
    "crop_compare": False, "compare": False,
    "mono-open": False, "dual-open": False, "mono-cut-open": False,
    "dual-cut-open": False, "crop-compare-open": False, "compare-open": False,
    "pdf_w_offset": 40, "pdf_h_offset": 20, "pdf_offset_ratio": 5, "pdf_white_margin": 0,
}

OUTPUTS = (("mono", "纯译文", "仅包含翻译后的内容"),
           ("dual", "双语对照", "生成所选布局的原文与译文"),
           ("mono_cut", "译文裁剪", "纯译文从宽度 1/2 裁剪后上下拼接，适合手机阅读"),
           ("dual_cut", "双语裁剪", "双语文件从宽度 1/2 裁剪后上下拼接"),
           ("compare", "左右对照", "左边为原文，右边为译文"),
           ("crop_compare", "裁剪对照", "双栏 PDF 先裁剪为单栏，再左右拼接"))

BOOL_LABELS = {
    "transFirst": "译文优先", "ocr": "强制 OCR 兼容模式", "autoOcr": "自动 OCR 兼容模式",
    "noWatermark": "移除水印", "saveGlossary": "保存自动提取的术语表", "disableGlossary": "禁用自动术语提取（减少远程请求）",
    "noDual": "禁止生成双语文件", "noMono": "禁止生成纯译文文件", "skipClean": "跳过 PDF 清理",
    "disableRichTextTranslate": "禁用富文本翻译", "enhanceCompatibility": "增强 PDF 兼容性",
    "translateTableText": "翻译表格文字", "onlyIncludeTranslatedPage": "仅保留选中翻译的页面",
    "skipSubsetFonts": "跳过字体子集化", "babeldoc": "旧版使用 BabelDOC",
    "rename": "自动命名输出文件", "showProgress": "显示任务阶段详情",
}

SERVICES = ("siliconflowfree", "bing", "google", "openailiked", "AliyunDashScope", "deepseek", "openai",
            "gemini", "silicon", "zhipu", "ModelScope", "qwen-mt", "deepl", "deeplx", "tencent", "grok",
            "dify", "argos", "ollama", "xinference", "anythingllm", "azure-openai", "azure", "groq", "claudecode")


def concurrency_preset(options, optimized):
    result = copy.deepcopy(options)
    qps = max(1, int(result['qps']))
    result['poolSize'] = min(64, qps * 3) if optimized else 0
    return result


def load_options(settings):
    options = copy.deepcopy(DEFAULTS)
    stored = settings.get("translation", {})
    if isinstance(stored, dict):
        for key, default in DEFAULTS.items():
            value = stored.get(key, default)
            if isinstance(default, bool):
                options[key] = value is True or str(value).lower() in ("true", "1")
            elif isinstance(default, int):
                try:
                    options[key] = int(value)
                except (ValueError, TypeError):
                    pass
            else:
                options[key] = str(value)
    return options


def validate_options(options):
    result = copy.deepcopy(options)
    for key, low, high in (("qps", 1, 1000), ("poolSize", 0, 1000), ("threadNum", 1, 1000), ("skipLastPages", 0, 100000)):
        try:
            result[key] = int(result[key])
            if not low <= result[key] <= high:
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError(f"{key} 应在 {low}–{high} 之间") from None
    for key in ("sourceLang", "targetLang"):
        result[key] = str(result[key]).strip()
        if len(result[key]) < 2:
            raise ValueError("请输入有效的源语言和目标语言代码")
    if result["engine"] not in ("pdf2zh", "pdf2zh_next"):
        raise ValueError("请选择有效的翻译引擎")
    if result["dualMode"] not in ("LR", "TB"):
        raise ValueError("双语布局应为 LR 或 TB")
    if not any(result[key] for key, _, _ in OUTPUTS):
        raise ValueError("请至少选择一种输出格式")
    for key in ("pdf_w_offset", "pdf_h_offset", "pdf_white_margin"):
        result[key] = int(result[key])
        if result[key] < 0:
            raise ValueError("裁剪边距不能为负数")
    result["pdf_offset_ratio"] = float(result["pdf_offset_ratio"])
    if not 0 < result["pdf_offset_ratio"] < 1000:
        raise ValueError("裁剪比例应大于 0 且小于 1000")
    return result


def build_payload(options, profiles):
    result = validate_options(options)
    service = result["service"] if result["engine"] == "pdf2zh" else result["next_service"]
    profile = copy.deepcopy(profiles.get(f"{result['engine']}:{service}", {}))
    extra = profile.get("extraData", {})
    if not isinstance(extra, dict):
        raise ValueError("API 附加参数必须是 JSON 对象")
    result["llm_api"] = profile
    if result["engine"] == "pdf2zh_next":
        # Generate only files needed for the requested postprocessing.
        need_mono = result["mono"] or result["mono_cut"]
        need_dual = any(result[key] for key in ("dual", "dual_cut", "compare", "crop_compare"))
        # Home-page output choices are authoritative, including older saved flags.
        result["noMono"], result["noDual"] = not need_mono, not need_dual
    return result


def read_profiles(config_dir):
    """Seed desktop API entries from existing config without rewriting it."""
    import toml
    from utils.config_map import pdf2zh_config_map, pdf2zh_next_config_map, pdf2zh_next_service_aliases
    profiles = {}
    for engine, name, mapping in (("pdf2zh", "config.json", pdf2zh_config_map), ("pdf2zh_next", "config.toml", pdf2zh_next_config_map)):
        path = Path(config_dir) / name
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8")) if engine == "pdf2zh" else toml.loads(path.read_text(encoding="utf-8"))
        for service in SERVICES:
            canonical = pdf2zh_next_service_aliases.get(service, service) if engine == "pdf2zh_next" else service
            fields = mapping.get(canonical, {})
            section = data.get(canonical + "_detail", {}) if engine == "pdf2zh_next" else next((item.get("envs", {}) for item in data.get("translators", []) if item.get("name") == canonical), {})
            profile = {key: section.get(fields[key], "") for key in ("apiKey", "apiUrl", "model") if key in fields}
            profile["extraData"] = {key: section[key] for key in fields.get("extraData", []) if key in section and section[key] != "null"}
            profiles[f"{engine}:{service}"] = profile
    return profiles
