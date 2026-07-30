import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import zhHant, { Dict } from "./zh-Hant";
import zhHans from "./zh-Hans";
import en from "./en";
import ja from "./ja";
import ko from "./ko";

/** UI localisation. Every visible string goes through `t()`; new development
 *  must add its keys to zh-Hant.ts (the canonical dict — the other locales are
 *  typed against it, so a missing translation is a build error). */

export type Lang = "zh-Hant" | "zh-Hans" | "en" | "ja" | "ko";

export const LANGS: { code: Lang; label: string }[] = [
  { code: "zh-Hant", label: "繁體中文" },
  { code: "zh-Hans", label: "简体中文" },
  { code: "en", label: "English" },
  { code: "ja", label: "日本語" },
  { code: "ko", label: "한국어" },
];

const DICTS: Record<Lang, Dict> = { "zh-Hant": zhHant, "zh-Hans": zhHans, en, ja, ko };
const STORE_KEY = "bastet.lang";

/** Browser preference → supported locale. Region tags matter for Chinese:
 *  TW/HK/MO are traditional, everything else simplified. */
export function detectLang(): Lang {
  const stored = localStorage.getItem(STORE_KEY);
  if (stored && stored in DICTS) return stored as Lang;
  const prefs = navigator.languages?.length
    ? navigator.languages : [navigator.language || "en"];
  for (const raw of prefs) {
    const tag = raw.toLowerCase();
    if (tag.startsWith("zh")) {
      return /hant|tw|hk|mo/.test(tag) ? "zh-Hant" : "zh-Hans";
    }
    if (tag.startsWith("ja")) return "ja";
    if (tag.startsWith("ko")) return "ko";
    if (tag.startsWith("en")) return "en";
  }
  return "en";
}

export type Vars = Record<string, string | number>;
export type T = (key: keyof Dict | string, vars?: Vars, fallback?: string) => string;

type Ctx = { lang: Lang; setLang: (l: Lang) => void; t: T };
const I18nCtx = createContext<Ctx | null>(null);

function format(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name) =>
    name in vars ? String(vars[name]) : whole);
}

export function I18nProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>(() => detectLang());

  useEffect(() => { document.documentElement.lang = lang; }, [lang]);

  const setLang = useCallback((next: Lang) => {
    localStorage.setItem(STORE_KEY, next);
    setLangState(next);
  }, []);

  const t = useCallback<T>((key, vars, fallback) => {
    const dict = DICTS[lang] as Record<string, string | undefined>;
    const template = dict[key as string]
      ?? (DICTS.en as Record<string, string | undefined>)[key as string];
    return format(template ?? fallback ?? String(key), vars);
  }, [lang]);

  const value = useMemo(() => ({ lang, setLang, t }), [lang, setLang, t]);
  return <I18nCtx.Provider value={value}>{children}</I18nCtx.Provider>;
}

export function useI18n(): Ctx {
  const ctx = useContext(I18nCtx);
  if (!ctx) throw new Error("useI18n outside I18nProvider");
  return ctx;
}

/** Convenience: just the translate function. */
export function useT(): T {
  return useI18n().t;
}

/** Workflow vocabulary comes from the backend keyed by stable ids (`reviewer`,
 *  `tests-pass`); we localise the label and fall back to whatever the server
 *  sent for ids we don't know (user-defined roles). */
export function useVocab() {
  const t = useT();
  return {
    roleLabel: (id?: string | null, fallback?: string) =>
      id ? t(`wfrole.${id}`, undefined, fallback ?? id) : (fallback ?? ""),
    gateLabel: (id: string, fallback?: string) =>
      t(`wfgate.${id}`, undefined, fallback ?? id),
  };
}

export function LanguagePicker() {
  const { lang, setLang, t } = useI18n();
  return (
    <select className="lang-picker" value={lang} title={t("app.lang")}
            onChange={(e) => setLang(e.target.value as Lang)}>
      {LANGS.map((l) => <option key={l.code} value={l.code}>{l.label}</option>)}
    </select>
  );
}
