import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import {
    assertBackendCapabilities,
    buildTranslationRequest,
    PLUGIN_TRANSLATION_PARAMETERS,
} from "../src/modules/pdf2zhProtocol.ts";

test("plugin request carries document options and never carries credentials or backend selectors", () => {
    const result = buildTranslationRequest(
        { fileName: "paper.pdf", base64: "JVBERg==" },
        {
            mono: "true",
            dual: "false",
            fontFamily: "serif",
            skipLastPages: "2",
            engine: "pdf2zh",
            service: "untrusted",
            next_service: "untrusted",
            sourceLang: "ja",
            targetLang: "fr",
            qps: 999,
            serverUrl: "http://127.0.0.1:8890",
            model: "wrong-model",
            apiKey: "private-fixture-key",
            llm_api: { apiKey: "private-fixture-key" },
        },
    );
    assert.equal(result.mono, "true");
    assert.equal(result.fontFamily, "serif");
    assert.equal(result.translationConfigSource, "backend-v1");
    assert.equal(result.asyncJob, true);
    const allowed = new Set<string>([
        ...PLUGIN_TRANSLATION_PARAMETERS,
        "fileName",
        "fileContent",
        "translationConfigSource",
        "asyncJob",
    ]);
    assert.ok(Object.keys(result).every((key) => allowed.has(key)));
    assert.ok(!JSON.stringify(result).includes("private-fixture-key"));
    assert.ok(!("engine" in result));
    assert.ok(!("serverUrl" in result));
});

test("unsupported backend cannot silently fall back to its old translator defaults", () => {
    for (const payload of [
        null,
        {},
        { status: "ok", version: "4.2.1" },
        { status: "ok", capabilities: { backendTranslationConfig: "true" } },
    ]) {
        assert.throws(() => assertBackendCapabilities(payload), /v4\.3\.0/);
    }
    assert.doesNotThrow(() =>
        assertBackendCapabilities({
            status: "ok",
            capabilities: { backendTranslationConfig: true },
        }),
    );
});

test("preferences retain exactly the screenshot options and the requested attribution", () => {
    const page = readFileSync(
        new URL("../addon/content/preferences.xhtml", import.meta.url),
        "utf8",
    );
    const actual = [
        ...page.matchAll(
            /preference="extensions\.zotero\.__addonRef__\.([^\"]+)"/g,
        ),
    ].map((match) => match[1]);
    const expected = [
        "skipLastPages",
        "rename",
        "showProgress",
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
        "enhanceCompatibility",
        "disableRichTextTranslate",
        "translateTableText",
        "onlyIncludeTranslatedPage",
        ...[
            "mono",
            "dual",
            "mono-cut",
            "dual-cut",
            "crop-compare",
            "compare",
        ].flatMap((key) => [key, key + "-open"]),
    ];
    assert.deepEqual(actual.sort(), expected.sort());
    assert.match(page, /插件基于Zotero PDF2zh二次开发，源码来自/);
    assert.match(page, /关于本二开插件的所有信息，请访问/);
    assert.match(page, /本插件为免费开源插件，请勿在其他渠道购买本插件/);
    assert.match(page, /PDF2zh for Zotero __buildVersion__/);
    assert.ok(
        page.includes('href="https://github.com/guaguastandup/zotero-pdf2zh"'),
    );
    assert.ok(page.includes('href="https://github.com/erxinzi/PDF2zh"'));
    assert.ok(!page.includes("llmapi-table"));
});
