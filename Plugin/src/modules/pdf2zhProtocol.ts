// File data and these options are the entire new plugin request contract.
export const PLUGIN_TRANSLATION_PARAMETERS = [
    "skipLastPages",
    "transFirst",
    "fontFamily",
    "dualMode",
    "ocr",
    "autoOcr",
    "noWatermark",
    "saveGlossary",
    "disableGlossary",
    "noDual",
    "noMono",
    "skipClean",
    "disableRichTextTranslate",
    "enhanceCompatibility",
    "translateTableText",
    "onlyIncludeTranslatedPage",
    "mono",
    "dual",
    "mono_cut",
    "dual_cut",
    "crop_compare",
    "compare",
] as const;

export function buildTranslationRequest(
    file: { fileName: string; base64: string },
    options: object,
) {
    const request: Record<string, unknown> = {
        fileName: file.fileName,
        fileContent: file.base64,
        translationConfigSource: "backend-v1",
        asyncJob: true,
    };
    const values = options as Record<string, unknown>;
    for (const key of PLUGIN_TRANSLATION_PARAMETERS) {
        if (values[key] !== undefined) request[key] = values[key];
    }
    return request;
}

export function assertBackendCapabilities(payload: unknown) {
    const health = payload as {
        status?: string;
        capabilities?: { backendTranslationConfig?: boolean };
    } | null;
    if (
        health?.status !== "ok" ||
        health.capabilities?.backendTranslationConfig !== true
    ) {
        throw new Error(
            "请更新 PDF2zh 后端至 v4.3.0 或以上，并在 EXE 中保存翻译设置后重试。",
        );
    }
}
