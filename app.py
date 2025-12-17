# datacure_cleaning_app.py
# -----------------------------------------------------------------------------
# Datacure – prototype Streamlit
# - Import (CSV / Excel / JSON / Stata)
# - Aperçu du dataset (source de vérité: st.session_state["df"])
# - 2 modes : méthodologique (guidé) vs libre
# - Nettoyage simple (méthodologique) : standardisation texte + NA par variable (validation bouton)
# - Q&A dataset (principalement déterministe)
# - Option IA : générer code pandas de nettoyage (mode libre)
# - Export CSV + (optionnel) codebook PDF
# -----------------------------------------------------------------------------

from __future__ import annotations

import io
import os
import unicodedata
from datetime import datetime
from typing import Optional, Tuple

import pandas as pd
import streamlit as st
from openai import OpenAI


# === PDF (optionnel) ===
try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    _HAS_REPORTLAB = True
except Exception:
    _HAS_REPORTLAB = False


# === Streamlit ===
st.set_page_config(page_title="Datacure Prototype", layout="wide")
st.title("Datacure - Assistant de nettoyage de données (v0)")


# === OpenAI key ===
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    try:
        api_key = st.secrets.get("OPENAI_API_KEY")
    except Exception:
        api_key = None

client: Optional[OpenAI] = OpenAI(api_key=api_key) if api_key else None


# === Upload ===
uploaded_file = st.file_uploader(
    "Charge un fichier de données",
    type=["csv", "xlsx", "xls", "json", "dta"],
)


def _remove_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


def _standardize_text_value(
    x,
    remove_accents: bool,
    acronyms: set[str],
    style: str,
    remove_double_spaces: bool = True,
) -> object:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return x
    if not isinstance(x, str):
        return x

    s = x.strip()
    if not s:
        return s

    if remove_accents:
        s = _remove_accents(s)

    if remove_double_spaces:
        s = " ".join(s.split())

    if style == "Commencer par une majuscule":
        s = s.lower().capitalize()
    elif style == "Tout en MAJUSCULES":
        s = s.upper()
    elif style == "Tout en minuscules":
        s = s.lower()
    else:
        s = s.lower().title()

    if acronyms:
        tokens = s.split(" ")
        tokens = [t.upper() if t.upper() in acronyms else t for t in tokens]
        s = " ".join(tokens)

    return s


def _text_columns(dataframe: pd.DataFrame) -> list[str]:
    return [
        c
        for c in dataframe.columns
        if dataframe[c].dtype == "object" or str(dataframe[c].dtype) == "string"
    ]


def load_data(file) -> Tuple[pd.DataFrame, str]:
    filename = (getattr(file, "name", "") or "").lower().strip()

    if filename.endswith(".csv"):
        return pd.read_csv(file), "csv"

    if filename.endswith((".xls", ".xlsx")):
        try:
            xls = pd.ExcelFile(file)
        except ImportError as e:
            raise ImportError(
                "Missing optional dependency 'openpyxl'. Installe-le avec: pip install openpyxl"
            ) from e
        sheet = st.selectbox("Choisis une feuille Excel", xls.sheet_names, index=0)
        return pd.read_excel(xls, sheet_name=sheet), "excel"

    if filename.endswith(".json"):
        try:
            return pd.read_json(file), "json"
        except ValueError:
            try:
                file.seek(0)
            except Exception:
                pass
            return pd.read_json(file, lines=True), "json"

    if filename.endswith(".dta"):
        return pd.read_stata(file), "stata"

    raise ValueError("Format non supporté. Utilise CSV, Excel, JSON ou Stata (.dta).")


def _reset_to_uploaded_file() -> None:
    uploaded_file.seek(0)
    df0, ft0 = load_data(uploaded_file)
    st.session_state["df"] = df0
    st.session_state["file_type"] = ft0


def _ensure_state() -> None:
    st.session_state.setdefault("cleaning_log", [])
    st.session_state.setdefault("missing_decisions", {})
    st.session_state.setdefault("missing_processed", set())


def _log_event(**kwargs) -> None:
    st.session_state["cleaning_log"].append(
        {"ts": datetime.now().isoformat(timespec="seconds"), **kwargs}
    )


def _infer_semantic_type(s: pd.Series) -> str:
    if s.dtype == "bool":
        return "Catégorielle"

    if pd.api.types.is_numeric_dtype(s):
        nunique = int(s.nunique(dropna=True))
        n = int(s.notna().sum())
        if n == 0:
            return "Continue"

        if nunique <= 12:
            return "Catégorielle"

        if (nunique / max(n, 1)) <= 0.05:
            return "Catégorielle"

        return "Continue"

    return "Catégorielle"


def _detect_special_codes(s: pd.Series) -> list[tuple[str, int]]:
    candidates = {
        -9,
        -8,
        -7,
        -6,
        -5,
        -4,
        -3,
        -2,
        -1,
        88,
        99,
        888,
        999,
        9999,
        777,
        666,
    }

    out: list[tuple[str, int]] = []

    try:
        if pd.api.types.is_numeric_dtype(s):
            for v in candidates:
                cnt = int((s == v).sum())
                if cnt > 0:
                    out.append((str(v), cnt))
        else:
            text_candidates = {
                "NA",
                "N/A",
                "NULL",
                "null",
                "Unknown",
                "unknown",
                "-9",
                "-4",
                "99",
                "888",
                "999",
            }
            s2 = s.astype("string")
            for v in text_candidates:
                cnt = int((s2 == v).sum())
                if cnt > 0:
                    out.append((v, cnt))
    except Exception:
        return []

    out.sort(key=lambda x: x[1], reverse=True)
    return out[:6]


def _truncate(s: str, max_len: int) -> str:
    s = s or ""
    s = " ".join(s.split())
    return (s[: max_len - 1] + "…") if len(s) > max_len else s


def _col_profile(df_: pd.DataFrame, col: str) -> dict:
    s = df_[col]
    n = len(df_)
    na = int(s.isna().sum())
    nunique = int(s.nunique(dropna=True))

    semantic = _infer_semantic_type(s)

    profile = {
        "column": col,
        "dtype": str(s.dtype),
        "type": semantic,
        "missing": na,
        "missing_pct": round((na / n * 100) if n else 0.0, 2),
        "unique": nunique,
    }

    try:
        vc = s.value_counts(dropna=True)
        top_vals = [str(v) for v in vc.head(5).index.tolist()]
    except Exception:
        top_vals = []

    profile["examples"] = _truncate(", ".join(top_vals), 70)

    specials = _detect_special_codes(s)
    profile["special_codes"] = _truncate(", ".join([f"{v} (n={c})" for v, c in specials]), 60) if specials else ""

    if semantic == "Continue" and pd.api.types.is_numeric_dtype(s) and s.notna().any():
        profile["min"] = float(s.min(skipna=True))
        profile["max"] = float(s.max(skipna=True))
        profile["mean"] = float(s.mean(skipna=True))
        profile["median"] = float(s.median(skipna=True))
    else:
        profile["min"] = profile["max"] = profile["mean"] = profile["median"] = None

    return profile


def _make_codebook_pdf(df_: pd.DataFrame, dataset_name: str) -> bytes:
    if not _HAS_REPORTLAB:
        raise ModuleNotFoundError("reportlab")

    styles = getSampleStyleSheet()
    story = [Paragraph(dataset_name, styles["Title"]), Spacer(1, 0.3 * cm)]

    n_rows, n_cols = df_.shape
    summary_tbl = Table(
        [["Lignes", f"{n_rows:,}"], ["Colonnes", f"{n_cols:,}"]],
        colWidths=[5 * cm, 10 * cm],
    )
    summary_tbl.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
                ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                ("FONTNAME", (0, 0), (-1, -1), "Helvetica"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story += [
        summary_tbl,
        Spacer(1, 0.4 * cm),
        Paragraph("Dictionnaire des variables", styles["Heading2"]),
        Spacer(1, 0.2 * cm),
    ]

    header = [
        "Variable",
        "Type (analyse)",
        "Manquants",
        "%",
        "Uniques",
        "Codes spéciaux",
        "Exemples (top 5)",
        "Min",
        "Max",
        "Moy.",
        "Med.",
    ]

    def _table(rows_):
        tbl = Table(
            rows_,
            colWidths=[
                3.6 * cm,
                2.2 * cm,
                1.5 * cm,
                1.1 * cm,
                1.3 * cm,
                2.6 * cm,
                5.4 * cm,
                1.1 * cm,
                1.1 * cm,
                1.1 * cm,
                1.1 * cm,
            ],
            repeatRows=1,
        )
        tbl.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                ]
            )
        )
        return tbl

    rows = [header]
    for i, col in enumerate(df_.columns, start=1):
        p = _col_profile(df_, str(col))
        rows.append(
            [
                p["column"],
                p["type"],
                str(p["missing"]),
                str(p["missing_pct"]),
                str(p["unique"]),
                p["special_codes"],
                p["examples"],
                "" if p["min"] is None else f"{p['min']:.4g}",
                "" if p["max"] is None else f"{p['max']:.4g}",
                "" if p["mean"] is None else f"{p['mean']:.4g}",
                "" if p["median"] is None else f"{p['median']:.4g}",
            ]
        )
        if i % 25 == 0:
            story.append(_table(rows))
            story.append(PageBreak())
            rows = [header]

    if len(rows) > 1:
        story.append(_table(rows))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        leftMargin=1.2 * cm,
        rightMargin=1.2 * cm,
        topMargin=1.2 * cm,
        bottomMargin=1.2 * cm,
        title=dataset_name,
    )
    doc.build(story)
    return buf.getvalue()


# === Pas de fichier ===
if not uploaded_file:
    st.info("📂 Veuillez charger un fichier (CSV, Excel, JSON ou Stata) pour commencer.")
    st.stop()


# === Lecture + session state ===
try:
    df_in, file_type_in = load_data(uploaded_file)
except Exception as e:
    st.error(f"Erreur de lecture du fichier : {e}")
    st.stop()

uploaded_name = getattr(uploaded_file, "name", None)
if st.session_state.get("uploaded_name") != uploaded_name:
    st.session_state["df"] = df_in
    st.session_state["file_type"] = file_type_in
    st.session_state["uploaded_name"] = uploaded_name
    st.session_state.pop("generated_code", None)
    st.session_state.pop("missing_decisions", None)
    st.session_state.pop("missing_processed", None)
    st.session_state.pop("cleaning_log", None)

_ensure_state()

df = st.session_state["df"]
file_type = st.session_state.get("file_type", file_type_in)


# === Aperçu ===
st.subheader("Aperçu du fichier")
st.caption(f"📄 Format détecté : {file_type}")
st.dataframe(df.head())


# === Mode ===
mode = st.radio(
    "Mode de travail",
    ["🧭 Nettoyage méthodologique", "🧪 Mode libre"],
    horizontal=True,
    key="mode",
)


# ================================
# 🧭 MODE MÉTHODOLOGIQUE (simple)
# ================================
if mode == "🧭 Nettoyage méthodologique":
    with st.expander("🧭 Nettoyage simple", expanded=True):
        st.caption("Nettoyage sûr (bouton par bouton), sans interprétation statistique")

        # --- 1) Standardiser le texte ---
        st.markdown("### 1) 🧹 Standardiser le texte")
        cols_text = _text_columns(df)

        style = st.selectbox(
            "Style",
            [
                "Majuscule à chaque mot",
                "Commencer par une majuscule",
                "Tout en MAJUSCULES",
                "Tout en minuscules",
            ],
            index=0,
            key="m_std_style",
        )

        remove_acc = st.checkbox("Supprimer les accents", value=True, key="m_std_acc")
        remove_double_spaces = st.checkbox(
            "Supprimer les doubles espaces", value=True, key="m_std_spaces"
        )

        acronyms_raw = st.text_input(
            "Acronymes à garder en MAJ (séparés par des virgules)",
            value="",
            key="m_std_acronyms",
        )
        acronyms = {a.strip().upper() for a in acronyms_raw.split(",") if a.strip()}

        scope = st.radio(
            "Appliquer sur",
            ["Tout le tableau", "Une colonne", "Une ligne"],
            horizontal=True,
            key="m_std_scope",
        )

        selected_col: Optional[str] = None
        if scope == "Une colonne":
            selected_col = st.selectbox(
                "Colonne",
                cols_text if cols_text else ["(aucune colonne texte)"],
                key="m_std_col",
            )
            if not cols_text:
                selected_col = None

        selected_row: Optional[int] = None
        if scope == "Une ligne":
            selected_row = int(
                st.number_input(
                    "Index de ligne (0 = première ligne)",
                    min_value=0,
                    max_value=max(0, len(df) - 1),
                    value=0,
                    step=1,
                    key="m_std_row",
                )
            )

        c1, c2 = st.columns(2)
        with c1:
            if st.button(
                "✨ Appliquer la standardisation",
                use_container_width=True,
                key="m_std_apply",
            ):
                if not cols_text:
                    st.warning("Aucune colonne texte détectée.")
                else:
                    df2 = df.copy()
                    if scope == "Tout le tableau":
                        for c in cols_text:
                            df2[c] = df2[c].apply(
                                lambda v: _standardize_text_value(
                                    v,
                                    remove_acc,
                                    acronyms,
                                    style,
                                    remove_double_spaces,
                                )
                            )
                        _log_event(
                            step="standardiser_texte",
                            scope="tableau",
                            columns=cols_text,
                            options={
                                "style": style,
                                "remove_acc": remove_acc,
                                "remove_double_spaces": remove_double_spaces,
                                "acronyms": sorted(list(acronyms)),
                            },
                        )
                    elif scope == "Une colonne" and selected_col:
                        df2[selected_col] = df2[selected_col].apply(
                            lambda v: _standardize_text_value(
                                v,
                                remove_acc,
                                acronyms,
                                style,
                                remove_double_spaces,
                            )
                        )
                        _log_event(
                            step="standardiser_texte",
                            scope="colonne",
                            column=selected_col,
                            options={
                                "style": style,
                                "remove_acc": remove_acc,
                                "remove_double_spaces": remove_double_spaces,
                                "acronyms": sorted(list(acronyms)),
                            },
                        )
                    elif scope == "Une ligne" and selected_row is not None:
                        r = selected_row
                        for c in cols_text:
                            df2.at[df2.index[r], c] = _standardize_text_value(
                                df2.at[df2.index[r], c],
                                remove_acc,
                                acronyms,
                                style,
                                remove_double_spaces,
                            )
                        _log_event(
                            step="standardiser_texte",
                            scope="ligne",
                            row_index=int(r),
                            columns=cols_text,
                            options={
                                "style": style,
                                "remove_acc": remove_acc,
                                "remove_double_spaces": remove_double_spaces,
                                "acronyms": sorted(list(acronyms)),
                            },
                        )

                    st.session_state["df"] = df2
                    st.success("✅ Standardisation appliquée")
                    st.rerun()

        with c2:
            if st.button("↩️ Annuler les changements", use_container_width=True, key="m_reset"):
                _reset_to_uploaded_file()
                st.session_state["missing_decisions"] = {}
                st.session_state["missing_processed"] = set()
                st.session_state["cleaning_log"] = []
                st.success("✅ Réinitialisé")
                st.rerun()

        st.divider()

        # --- 2) Valeurs manquantes (par variable) ---
        st.markdown("### 2) 🟡 Valeurs manquantes (par variable)")

        n_rows = len(df)
        miss = df.isna().sum().astype(int)
        miss_pct = (miss / n_rows * 100).round(2) if n_rows else (miss * 0.0)

        miss_tbl = pd.DataFrame(
            {
                "Variable": miss.index.astype(str),
                "Type": df.dtypes.astype(str).values,
                "NA (n)": miss.values,
                "NA (%)": miss_pct.values,
                "État": [
                    "✅ validé" if str(c) in st.session_state["missing_processed"] else "⏳ à décider"
                    for c in miss.index.astype(str)
                ],
            }
        ).sort_values(["NA (n)", "Variable"], ascending=[False, True])

        def _style_missing(row):
            try:
                p = float(row["NA (%)"])
            except Exception:
                p = 0.0
            return ["font-weight:600"] * len(row) if p >= 20 else [""] * len(row)

        st.dataframe(miss_tbl.style.apply(_style_missing, axis=1), use_container_width=True)
        st.caption(
            "Astuce: les décisions s'appliquent immédiatement au dataset (suppression de lignes), variable par variable."
        )

        cols_with_na = [c for c in df.columns if int(df[c].isna().sum()) > 0]
        if not cols_with_na:
            st.success("Aucune valeur manquante détectée.")
        else:
            st.markdown("#### Décider et valider (bouton par variable)")

            for col in cols_with_na:
                col_name = str(col)
                na_n = int(df[col].isna().sum())
                na_p = float((na_n / len(df) * 100) if len(df) else 0.0)

                is_done = col_name in st.session_state["missing_processed"]

                with st.container():
                    left, right = st.columns([3, 2])

                    with left:
                        st.write(f"**{col_name}** — NA: {na_n:,} ({na_p:.2f}%)")

                    with right:
                        decision_key = f"miss_dec__{col_name}"
                        btn_key = f"miss_apply__{col_name}"

                        default_dec = st.session_state["missing_decisions"].get(
                            col_name, "Ne rien faire (garder les NA)"
                        )

                        decision = st.radio(
                            "Décision",
                            [
                                "Ne rien faire (garder les NA)",
                                f"Exclure les lignes où '{col_name}' est manquant",
                                f"Marquer '{col_name}' comme analyse en cas complets",
                            ],
                            index=[
                                "Ne rien faire (garder les NA)",
                                f"Exclure les lignes où '{col_name}' est manquant",
                                f"Marquer '{col_name}' comme analyse en cas complets",
                            ].index(default_dec),
                            key=decision_key,
                            disabled=is_done,
                            label_visibility="collapsed",
                        )

                        if st.button(
                            "✅ Valider",
                            use_container_width=True,
                            key=btn_key,
                            disabled=is_done,
                        ):
                            st.session_state["missing_decisions"][col_name] = decision

                            before = len(df)
                            rows_removed = 0

                            if decision.startswith("Exclure les lignes"):
                                df2 = df.dropna(subset=[col])
                                rows_removed = before - len(df2)
                                st.session_state["df"] = df2
                                df = df2

                            st.session_state["missing_processed"].add(col_name)

                            _log_event(
                                step="valeurs_manquantes",
                                variable=col_name,
                                missing_n=int(na_n),
                                missing_pct=float(round(na_p, 2)),
                                decision=decision,
                                rows_removed=int(rows_removed),
                                rows_before=int(before),
                                rows_after=int(len(df)),
                            )

                            st.success(f"Décision validée pour '{col_name}'.")
                            st.rerun()

                    st.divider()


# ================================
# 🧪 MODE LIBRE
# ================================
else:
    with st.expander("💬 Poser une question sur le dataset", expanded=False):
        q = st.text_input(
            "Question",
            placeholder="Ex : Combien de lignes ? Y a-t-il des doublons d'identifiant ?",
            key="qa_q",
        )

        id_candidates = list(df.columns)
        id_col = st.selectbox("Colonne identifiant (si besoin)", id_candidates, key="qa_id")

        qa_out = st.empty()

        def _answer_question(question: str) -> None:
            qq = (question or "").strip().lower()
            if not qq:
                qa_out.info("Écris une question ci-dessus.")
                return

            if "combien" in qq and ("ligne" in qq or "rows" in qq):
                qa_out.success(f"Il y a {len(df):,} ligne(s).")
                return
            if "combien" in qq and ("colonne" in qq or "columns" in qq):
                qa_out.success(f"Il y a {df.shape[1]:,} colonne(s).")
                return
            if "dimension" in qq or "shape" in qq:
                qa_out.success(f"Dimensions : {df.shape[0]:,} lignes × {df.shape[1]:,} colonnes")
                return

            if "fi item id" in qq or "fi_item_id" in qq or "fi-item id" in qq:
                col2 = None
                if "FI Item ID" in df.columns:
                    col2 = "FI Item ID"
                else:
                    norm = {str(c).strip().lower(): c for c in df.columns}
                    col2 = norm.get("fi item id") or norm.get("fi_item_id") or norm.get("fi-item id")

                if not col2:
                    qa_out.error("Je ne trouve pas la colonne 'FI Item ID' dans ce dataset.")
                    return

                n_unique = int(df[col2].nunique(dropna=True))
                qa_out.success(f"'{col2}' contient {n_unique:,} identifiant(s) unique(s) (hors NA).")
                return

            if "doubl" in qq or "duplicate" in qq or ("identifi" in qq and "2 fois" in qq):
                if id_col not in df.columns:
                    qa_out.error("Je ne trouve pas la colonne identifiant sélectionnée.")
                    return
                s = df[id_col]
                dup_mask = s.duplicated(keep=False) & s.notna()
                n_dup_rows = int(dup_mask.sum())
                n_dup_ids = int(s[dup_mask].nunique(dropna=True))
                if n_dup_rows == 0:
                    qa_out.success(f"Aucun doublon détecté dans '{id_col}'.")
                else:
                    qa_out.warning(
                        f"Doublons détectés dans '{id_col}' : {n_dup_ids} identifiant(s) dupliqué(s), "
                        f"touchant {n_dup_rows} ligne(s)."
                    )
                    sample_ids = s[dup_mask].astype("string").dropna().unique()[:10]
                    qa_out.write("Exemples d'identifiants dupliqués :")
                    qa_out.write(list(sample_ids))
                return

            if "manquant" in qq or "missing" in qq or " na" in f" {qq} ":
                na_counts = df.isna().sum().sort_values(ascending=False)
                top = na_counts[na_counts > 0].head(20)
                if top.empty:
                    qa_out.success("Aucune valeur manquante détectée.")
                else:
                    qa_out.warning("Colonnes avec des valeurs manquantes (top 20) :")
                    qa_out.dataframe(top.to_frame(name="NA"))
                return

            if "unique" in qq or "distinct" in qq:
                if id_col in df.columns:
                    n_unique = int(df[id_col].nunique(dropna=True))
                    qa_out.success(f"'{id_col}' contient {n_unique:,} valeur(s) unique(s) (hors NA).")
                    return

            if client:
                dtypes_txt = df.dtypes.astype(str).to_dict()
                preview = df.head(20).to_dict(orient="records")
                prompt_qa = f"""Tu es un assistant d'analyse de données.
Réponds brièvement en français.
Ne fournis pas de code.

Question: {question}
Colonne_identifiant: {id_col}
dtypes: {dtypes_txt}
aperçu_20_lignes: {preview}
"""

                try:
                    resp = client.chat.completions.create(
                        model="gpt-3.5-turbo",
                        messages=[{"role": "user", "content": prompt_qa}],
                        temperature=0,
                    )
                    qa_out.info(resp.choices[0].message.content.strip())
                except Exception as e:
                    qa_out.error(f"Erreur IA : {e}")
            else:
                qa_out.info("Question non reconnue. Essaie : lignes, colonnes, doublons, manquants.")

        if st.button("🤖 Répondre", use_container_width=True, key="qa_btn"):
            _answer_question(q)

    with st.expander("⚡ Commandes rapides", expanded=False):
        cols_text = _text_columns(df)

        style = st.selectbox(
            "Standardiser (style)",
            [
                "Majuscule à chaque mot",
                "Commencer par une majuscule",
                "Tout en MAJUSCULES",
                "Tout en minuscules",
            ],
            index=0,
            key="f_std_style",
        )
        remove_acc = st.checkbox("Supprimer les accents", value=True, key="f_std_acc")
        remove_double_spaces = st.checkbox(
            "Supprimer les doubles espaces", value=True, key="f_std_spaces"
        )
        acronyms_raw = st.text_input("Acronymes (virgules)", value="", key="f_std_acronyms")
        acronyms = {a.strip().upper() for a in acronyms_raw.split(",") if a.strip()}

        scope = st.radio(
            "Appliquer sur",
            ["Tout le tableau", "Une colonne", "Une ligne"],
            horizontal=True,
            key="f_std_scope",
        )
        selected_col: Optional[str] = None
        if scope == "Une colonne":
            selected_col = st.selectbox("Colonne", cols_text if cols_text else ["(aucune)"], key="f_std_col")
            if not cols_text:
                selected_col = None

        selected_row: Optional[int] = None
        if scope == "Une ligne":
            selected_row = int(
                st.number_input(
                    "Index de ligne",
                    min_value=0,
                    max_value=max(0, len(df) - 1),
                    value=0,
                    step=1,
                    key="f_std_row",
                )
            )

        a1, a2 = st.columns(2)
        with a1:
            if st.button("✨ Standardiser", use_container_width=True, key="f_std_apply"):
                if not cols_text:
                    st.warning("Aucune colonne texte détectée.")
                else:
                    df2 = df.copy()
                    if scope == "Tout le tableau":
                        for c in cols_text:
                            df2[c] = df2[c].apply(
                                lambda v: _standardize_text_value(
                                    v,
                                    remove_acc,
                                    acronyms,
                                    style,
                                    remove_double_spaces,
                                )
                            )
                    elif scope == "Une colonne" and selected_col:
                        df2[selected_col] = df2[selected_col].apply(
                            lambda v: _standardize_text_value(
                                v,
                                remove_acc,
                                acronyms,
                                style,
                                remove_double_spaces,
                            )
                        )
                    elif scope == "Une ligne" and selected_row is not None:
                        r = selected_row
                        for c in cols_text:
                            df2.at[df2.index[r], c] = _standardize_text_value(
                                df2.at[df2.index[r], c],
                                remove_acc,
                                acronyms,
                                style,
                                remove_double_spaces,
                            )
                    st.session_state["df"] = df2
                    st.rerun()

        with a2:
            if st.button("↩️ Reset", use_container_width=True, key="f_reset"):
                _reset_to_uploaded_file()
                st.rerun()

    if not client:
        st.info("(IA désactivée) Ajoute OPENAI_API_KEY pour activer le nettoyage via API.")

    user_input = st.text_input(
        "Commande IA (optionnel)",
        placeholder="Ex : supprime les lignes où age est manquant",
        key="nl_cmd",
    )

    if user_input and client:
        prompt = f"""
Tu es un assistant Python expert en nettoyage de données avec pandas.
Voici un DataFrame nommé df.
L'utilisateur demande : \"{user_input}\".

Contraintes:
- Retourne uniquement du code Python exécutable.
- Le code doit MODIFIER le DataFrame df et laisser df comme résultat final.
- N'utilise pas d'import.
- N'accède pas au système de fichiers.
- N'utilise pas de réseau.
""".strip()

        result_container = st.empty()

        with st.spinner("🧠 Génération du code Python..."):
            try:
                response = client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0,
                )
                st.session_state["generated_code"] = response.choices[0].message.content.strip()
            except Exception as e:
                result_container.error(f"❌ Erreur lors de l'appel à l'API OpenAI : {e}")

        if "generated_code" in st.session_state:
            code = st.session_state["generated_code"]
            with st.expander("🧠 Voir le code généré", expanded=False):
                st.code(code, language="python")

            if st.button("▶️ Exécuter", key="run_ai"):
                try:
                    local_vars = {"df": df.copy()}
                    exec(code, {}, local_vars)
                    if "df" not in local_vars:
                        raise RuntimeError("Le code généré n'a pas laissé de variable 'df' en sortie.")
                    st.session_state["df"] = local_vars["df"]
                    result_container.success("✅ Nettoyage appliqué")
                    st.rerun()
                except Exception as e:
                    result_container.error(f"❌ Erreur pendant l'exécution : {e}")


# === Codebook PDF (optionnel) ===
with st.expander("📄 Codebook (PDF)", expanded=False):
    if not _HAS_REPORTLAB:
        st.info("PDF indisponible : installe reportlab (pip install reportlab).")
    else:
        default_name = st.session_state.get("uploaded_name") or "dataset"
        pdf_title = st.text_input(
            "Titre du codebook",
            value=f"Codebook - {default_name}",
            key="pdf_title",
        )
        if st.button("📄 Générer le PDF", use_container_width=True, key="pdf_btn"):
            try:
                with st.spinner("Génération du codebook PDF..."):
                    pdf_bytes = _make_codebook_pdf(st.session_state["df"], dataset_name=pdf_title)
                st.download_button(
                    label="⬇️ Télécharger le codebook (PDF)",
                    data=pdf_bytes,
                    file_name="codebook.pdf",
                    mime="application/pdf",
                )
            except Exception as e:
                st.error(f"Erreur PDF : {e}")


# === Export CSV ===
df_final = st.session_state.get("df", df)
cleaned_csv = df_final.to_csv(index=False).encode("utf-8")

st.download_button(
    label="📥 Télécharger le fichier nettoyé (CSV)",
    data=cleaned_csv,
    file_name="fichier_nettoye.csv",
    mime="text/csv",
)


# === Mini-tests (optionnels) ===
if os.getenv("DATACURE_RUN_TESTS") == "1":
    import json

    class _FakeUpload:
        def __init__(self, name: str, payload: bytes):
            self.name = name
            self._bio = io.BytesIO(payload)

        def read(self, *args, **kwargs):
            return self._bio.read(*args, **kwargs)

        def seek(self, pos: int):
            return self._bio.seek(pos)

        def __getattr__(self, item):
            return getattr(self._bio, item)

    fake_csv = _FakeUpload("test.csv", b"a,b\n1,2\n")
    df_csv, t_csv = load_data(fake_csv)
    assert t_csv == "csv" and df_csv.shape == (1, 2)

    payload = json.dumps([{ "a": 1, "b": 2 }]).encode("utf-8")
    fake_json = _FakeUpload("test.json", payload)
    df_json, t_json = load_data(fake_json)
    assert t_json == "json" and df_json.shape == (1, 2)

    payload_jsonl = b"{\"a\": 1, \"b\": 2}\n{\"a\": 3, \"b\": 4}\n"
    fake_jsonl = _FakeUpload("test.json", payload_jsonl)
    df_jsonl, t_jsonl = load_data(fake_jsonl)
    assert t_jsonl == "json" and df_jsonl.shape == (2, 2)

    assert _standardize_text_value("  chu  bruxelles ", True, {"CHU"}, "Majuscule à chaque mot") == "CHU Bruxelles"
    assert _standardize_text_value("abc DEF", False, set(), "Tout en minuscules") == "abc def"
    assert _standardize_text_value("abc DEF", False, set(), "Tout en MAJUSCULES") == "ABC DEF"
    assert _standardize_text_value("abc DEF", False, set(), "Commencer par une majuscule") == "Abc def"
    assert _standardize_text_value("   ", False, set(), "Tout en MAJUSCULES") == ""

    # codes spéciaux
    s = pd.Series([1, 99, 99, None])
    assert _detect_special_codes(s)[0][0] == "99"

    st.success("✅ DATACURE_RUN_TESTS: tous les mini-tests ont réussi")
