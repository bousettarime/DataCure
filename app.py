# datacure_cleaning_app.py
# -----------------------------------------------------------------------------
# Je construis ici un prototype Streamlit “Data2C / Datacure” :
# - J'importe un fichier (CSV / Excel / JSON / Stata)
# - Je propose un bouton “Standardiser le texte” (tout / colonne / ligne)
# - Je propose des commandes rapides (ex: supprimer lignes avec NA)
# - Je peux demander à OpenAI de générer du code pandas pour un nettoyage
# - J'exécute ce code sur une copie du DataFrame et je propose le téléchargement
# -----------------------------------------------------------------------------

from __future__ import annotations

import io
import os
import unicodedata
from typing import Optional, Tuple

import pandas as pd
import streamlit as st
from openai import OpenAI

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak


# === Configuration Streamlit ===
st.set_page_config(page_title="Datacure Prototype", layout="wide")
st.title("Datacure - Assistant de nettoyage de données (v0)")


# === Clé OpenAI ===
# Je lis d'abord la variable d'environnement (dev), puis j'essaie Streamlit secrets (prod).
api_key = os.getenv("OPENAI_API_KEY")

if not api_key:
    try:
        api_key = st.secrets.get("OPENAI_API_KEY")
    except Exception:
        # Si aucun secrets.toml n'existe (local), Streamlit peut lever StreamlitSecretNotFoundError.
        api_key = None

client: Optional[OpenAI] = None
if not api_key:
    st.warning(
        "⚠️ Clé API OpenAI manquante. Configure-la dans .streamlit/secrets.toml "
        "ou comme variable d'environnement (OPENAI_API_KEY)."
    )
else:
    client = OpenAI(api_key=api_key)


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
    # Je laisse les valeurs manquantes et non-textuelles telles quelles
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return x
    if not isinstance(x, str):
        return x

    s = x.strip()
    if not s:
        return s

    if remove_accents:
        s = _remove_accents(s)

    # Je supprime les doubles (ou multiples) espaces si demandé
    if remove_double_spaces:
        s = " ".join(s.split())

    # J'applique le style demandé
    if style == "Commencer par une majuscule":
        s = s.lower().capitalize()
    elif style == "Tout en MAJUSCULES":
        s = s.upper()
    elif style == "Tout en minuscules":
        s = s.lower()
    else:
        # Par défaut : Majuscule à chaque mot
        s = s.lower().title()

    # Je force les acronymes spécifiés en MAJUSCULES, quel que soit le style
    if acronyms:
        tokens = s.split(" ")
        tokens = [t.upper() if t.upper() in acronyms else t for t in tokens]
        s = " ".join(tokens)

    return s

    if remove_accents:
        s = _remove_accents(s)

    # Je normalise les espaces
    s = " ".join(s.split())

    # J'applique le style demandé
    if style == "Commencer par une majuscule":
        s = s.lower().capitalize()
    elif style == "Tout en MAJUSCULES":
        s = s.upper()
    elif style == "Tout en minuscules":
        s = s.lower()
    else:
        # Par défaut : Majuscule à chaque mot
        s = s.lower().title()

    # Je force les acronymes spécifiés en MAJUSCULES, quel que soit le style
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
    """Je charge un fichier uploadé en DataFrame pandas et je retourne (df, file_type)."""

    filename = (getattr(file, "name", "") or "").lower().strip()

    if filename.endswith(".csv"):
        df = pd.read_csv(file)
        return df, "csv"

    if filename.endswith((".xls", ".xlsx")):
        xls = pd.ExcelFile(file)
        sheet = st.selectbox("Choisis une feuille Excel", xls.sheet_names, index=0)
        df = pd.read_excel(xls, sheet_name=sheet)
        return df, "excel"

    if filename.endswith(".json"):
        try:
            df = pd.read_json(file)
            return df, "json"
        except ValueError:
            try:
                file.seek(0)
            except Exception:
                pass
            df = pd.read_json(file, lines=True)
            return df, "json"

    if filename.endswith(".dta"):
        df = pd.read_stata(file)
        return df, "stata"

    raise ValueError("Format de fichier non supporté. Utilise CSV, Excel, JSON ou Stata (.dta).")


def _reset_to_uploaded_file() -> None:
    uploaded_file.seek(0)
    df0, ft0 = load_data(uploaded_file)
    st.session_state["df"] = df0
    st.session_state["file_type"] = ft0


def _col_profile(df_: pd.DataFrame, col: str) -> dict:
    s = df_[col]
    n = len(df_)
    na = int(s.isna().sum())
    nunique = int(s.nunique(dropna=True))
    dtype = str(s.dtype)

    profile = {
        "column": col,
        "dtype": dtype,
        "missing": na,
        "missing_pct": round((na / n * 100) if n else 0.0, 2),
        "unique": nunique,
    }

    # Exemples / top valeurs
    try:
        vc = s.value_counts(dropna=True)
        top_vals = [str(v) for v in vc.head(5).index.tolist()]
    except Exception:
        top_vals = []
    profile["examples"] = ", ".join(top_vals)

    # Stats numériques
    if pd.api.types.is_numeric_dtype(s):
        profile["min"] = float(s.min(skipna=True)) if s.notna().any() else None
        profile["max"] = float(s.max(skipna=True)) if s.notna().any() else None
        profile["mean"] = float(s.mean(skipna=True)) if s.notna().any() else None
        profile["median"] = float(s.median(skipna=True)) if s.notna().any() else None
    else:
        profile["min"] = profile["max"] = profile["mean"] = profile["median"] = None

    return profile


def _make_codebook_pdf(df_: pd.DataFrame, dataset_name: str = "Datacure codebook") -> bytes:
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(dataset_name, styles["Title"]))
    story.append(Spacer(1, 0.3 * cm))

    # Résumé dataset
    n_rows, n_cols = df_.shape
    summary_tbl = Table(
        [
            ["Lignes", f"{n_rows:,}"],
            ["Colonnes", f"{n_cols:,}"],
        ],
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
    story.append(summary_tbl)
    story.append(Spacer(1, 0.4 * cm))

    story.append(Paragraph("Dictionnaire des variables", styles["Heading2"]))
    story.append(Spacer(1, 0.2 * cm))

    # Profil par colonne
    rows = [[
        "Variable",
        "Type",
        "Manquants",
        "%",
        "Uniques",
        "Exemples (top 5)",
        "Min",
        "Max",
        "Moy.",
        "Med.",
    ]]

    for col in df_.columns:
        p = _col_profile(df_, str(col))
        rows.append(
            [
                p["column"],
                p["dtype"],
                str(p["missing"]),
                str(p["missing_pct"]),
                str(p["unique"]),
                p["examples"],
                "" if p["min"] is None else f"{p['min']:.4g}",
                "" if p["max"] is None else f"{p['max']:.4g}",
                "" if p["mean"] is None else f"{p['mean']:.4g}",
                "" if p["median"] is None else f"{p['median']:.4g}",
            ]
        )

        # Pagination simple : toutes les ~25 variables, je coupe
        if (len(rows) - 1) % 25 == 0 and (len(rows) - 1) != 0:
            tbl = Table(
                rows,
                colWidths=[
                    4.0 * cm,
                    2.0 * cm,
                    1.6 * cm,
                    1.2 * cm,
                    1.4 * cm,
                    6.0 * cm,
                    1.2 * cm,
                    1.2 * cm,
                    1.2 * cm,
                    1.2 * cm,
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
            story.append(tbl)
            story.append(PageBreak())
            rows = [rows[0]]

    # Dernière table
    tbl = Table(
        rows,
        colWidths=[
            4.0 * cm,
            2.0 * cm,
            1.6 * cm,
            1.2 * cm,
            1.4 * cm,
            6.0 * cm,
            1.2 * cm,
            1.2 * cm,
            1.2 * cm,
            1.2 * cm,
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
    story.append(tbl)

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


# === Lecture du fichier + session state ===
try:
    df_in, file_type_in = load_data(uploaded_file)
except Exception as e:
    st.error(f"Erreur de lecture du fichier : {e}")
    st.stop()

# Je garde l'état entre reruns
uploaded_name = getattr(uploaded_file, "name", None)
if st.session_state.get("uploaded_name") != uploaded_name:
    st.session_state["df"] = df_in
    st.session_state["file_type"] = file_type_in
    st.session_state["uploaded_name"] = uploaded_name
    st.session_state.pop("generated_code", None)
    st.session_state.pop("show_std_preview", None)

# Source de vérité
df = st.session_state["df"]
file_type = st.session_state.get("file_type", file_type_in)


# === Aperçu ===
st.subheader("Aperçu du fichier")
st.caption(f"📄 Format détecté : {file_type}")
st.dataframe(df.head())


# === Questions sur les données (sans modifier df) ===
with st.expander("💬 Poser une question sur le dataset", expanded=False):
    q = st.text_input(
        "Question",
        placeholder="Ex : Combien de lignes ? Y a-t-il des doublons d'identifiant ?",
    )

    id_candidates = list(df.columns)
    id_col = st.selectbox("Colonne identifiant (si besoin)", id_candidates)

    qa_out = st.empty()

    def _answer_question(question: str) -> None:
        qq = (question or "").strip().lower()
        if not qq:
            qa_out.info("Écris une question ci-dessus.")
            return

        # Dimensions
        if "combien" in qq and ("ligne" in qq or "rows" in qq):
            qa_out.success(f"Il y a {len(df):,} ligne(s).")
            return
        if "combien" in qq and ("colonne" in qq or "columns" in qq):
            qa_out.success(f"Il y a {df.shape[1]:,} colonne(s).")
            return
        if "dimension" in qq or "shape" in qq:
            qa_out.success(
                f"Dimensions : {df.shape[0]:,} lignes × {df.shape[1]:,} colonnes"
            )
            return

        # Duplicats d'identifiant
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

        # Valeurs manquantes
        if "manquant" in qq or "missing" in qq or "na" in qq:
            na_counts = df.isna().sum().sort_values(ascending=False)
            top = na_counts[na_counts > 0].head(20)
            if top.empty:
                qa_out.success("Aucune valeur manquante détectée.")
            else:
                qa_out.warning("Colonnes avec des valeurs manquantes (top 20) :")
                qa_out.dataframe(top.to_frame(name="NA"))
            return

        # Cas spécial : FI Item ID → je renvoie le nombre d'IDs uniques (hors NA)
        if "fi item id" in qq or "fi_item_id" in qq or "fi-item id" in qq:
            col = None
            # Je tente d'abord une correspondance exacte
            if "FI Item ID" in df.columns:
                col = "FI Item ID"
            else:
                # Sinon je cherche une correspondance insensible à la casse/espaces
                norm = {str(c).strip().lower(): c for c in df.columns}
                col = norm.get("fi item id") or norm.get("fi_item_id") or norm.get("fi-item id")

            if not col:
                qa_out.error("Je ne trouve pas la colonne 'FI Item ID' dans ce dataset.")
                return

            n_unique = int(df[col].nunique(dropna=True))
            qa_out.success(f"'{col}' contient {n_unique:,} identifiant(s) unique(s) (hors NA).")
            return

        # Valeurs uniques (générique sur la colonne ID sélectionnée)
        if "unique" in qq or "distinct" in qq:
            if id_col in df.columns:
                n_unique = int(df[id_col].nunique(dropna=True))
                qa_out.success(f"'{id_col}' contient {n_unique:,} valeur(s) unique(s) (hors NA).")
                return

        # Fallback (si clé API dispo)
        if client:
            # Je limite l'information envoyée : schéma + aperçu
            dtypes_txt = df.dtypes.astype(str).to_dict()
            preview = df.head(20).to_dict(orient="records")

            prompt_qa = f"""Tu es un assistant d'analyse de données.
Réponds brièvement en français.
Ne fournis pas de code.
Si la question demande un identifiant, utilise la colonne fournie.

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
            qa_out.info(
                "Je ne reconnais pas cette question. Essaie : lignes, colonnes, doublons, valeurs manquantes."
            )

    if st.button("🤖 Répondre", use_container_width=True):
        _answer_question(q)


# === Commandes rapides (sans API) ===
with st.expander("⚡ Commandes rapides", expanded=False):
    st.caption("Actions one-click pour nettoyer sans passer par l'API")

    # --- Standardiser le texte ---
    st.markdown("### 🧹 Standardiser le texte")

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
        key="std_style",
    )

    remove_acc = st.checkbox("Supprimer les accents", value=True, key="std_acc")
    remove_double_spaces = st.checkbox("Supprimer les doubles espaces", value=True, key="std_spaces")

    acronyms_raw = st.text_input(
        "Acronymes à garder en MAJ (séparés par des virgules)",
        value="",
        key="std_acronyms",
    )
    acronyms = {a.strip().upper() for a in acronyms_raw.split(",") if a.strip()}

    scope = st.radio(
        "Appliquer sur",
        ["Tout le tableau", "Une colonne", "Une ligne"],
        horizontal=True,
        key="std_scope",
    )

    selected_col: Optional[str] = None
    if scope == "Une colonne":
        if cols_text:
            selected_col = st.selectbox("Colonne", cols_text, key="std_col")
        else:
            st.info("Aucune colonne texte détectée.")

    selected_row: Optional[int] = None
    if scope == "Une ligne":
        selected_row = int(
            st.number_input(
                "Index de ligne (0 = première ligne)",
                min_value=0,
                max_value=max(0, len(df) - 1),
                value=0,
                step=1,
                key="std_row",
            )
        )

    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("✨ Standardiser", use_container_width=True, key="std_apply"):
            if not cols_text:
                st.warning("Je n'ai trouvé aucune colonne texte à standardiser.")
            else:
                if scope == "Tout le tableau":
                    for c in cols_text:
                        df[c] = df[c].apply(
                            lambda v: _standardize_text_value(
                                v, remove_acc, acronyms, style, remove_double_spaces
                            )
                        )

                elif scope == "Une colonne" and selected_col:
                    df[selected_col] = df[selected_col].apply(
                        lambda v: _standardize_text_value(
                            v, remove_acc, acronyms, style, remove_double_spaces
                        )
                    )

                elif scope == "Une ligne" and selected_row is not None:
                    r = selected_row
                    for c in cols_text:
                        df.at[df.index[r], c] = _standardize_text_value(
                            df.at[df.index[r], c],
                            remove_acc,
                            acronyms,
                            style,
                            remove_double_spaces,
                        )

                st.session_state["df"] = df
                st.success("✅ Standardisation appliquée")
                st.rerun()

    with c2:
        if st.button("↩️ Annuler les changements", use_container_width=True, key="std_reset"):
            _reset_to_uploaded_file()
            st.success("✅ Réinitialisé")
            st.rerun()

    preview_ph = st.empty()

    with c3:
        if st.button("👀 Voir un aperçu", use_container_width=True, key="std_preview"):
            st.session_state["show_std_preview"] = True

    if st.session_state.get("show_std_preview"):
        preview_ph.dataframe(st.session_state["df"].head())

    st.divider()

    # --- Suppression des valeurs manquantes ---
    st.markdown("### 🧽 Supprimer les lignes avec valeurs manquantes")

    missing_scope = st.radio(
        "Mode",
        [
            "N'importe quelle colonne (drop si au moins 1 NA)",
            "Une colonne",
            "Plusieurs colonnes",
        ],
        horizontal=False,
        key="na_scope",
    )

    cols_all = list(df.columns)
    col_one: Optional[str] = None
    cols_many: list[str] = []

    if missing_scope == "Une colonne":
        col_one = st.selectbox("Choisir la colonne", cols_all, key="na_one")

    if missing_scope == "Plusieurs colonnes":
        cols_many = st.multiselect("Choisir les colonnes", cols_all, key="na_many")

    m1, m2 = st.columns(2)

    with m1:
        if st.button("🧽 Supprimer", use_container_width=True, key="na_apply"):
            before = len(df)

            if missing_scope == "N'importe quelle colonne (drop si au moins 1 NA)":
                df = df.dropna(axis=0, how="any")

            elif missing_scope == "Une colonne" and col_one:
                df = df.dropna(subset=[col_one], how="any")

            elif missing_scope == "Plusieurs colonnes" and cols_many:
                df = df.dropna(subset=cols_many, how="any")

            st.session_state["df"] = df
            removed = before - len(df)
            st.success(f"✅ {removed} ligne(s) supprimée(s)")
            st.rerun()

    with m2:
        if st.button("📊 Compter les valeurs manquantes", use_container_width=True, key="na_count"):
            na_counts = df.isna().sum().sort_values(ascending=False)
            st.dataframe(na_counts.to_frame(name="NA").T)


# === Commande en langage naturel (API) ===
with st.expander("⚡ Commandes rapides", expanded=False):
    st.caption("Actions one-click pour nettoyer sans passer par l'API")

    missing_scope = st.radio(
        "Supprimer les lignes avec valeurs manquantes",
        [
            "N'importe quelle colonne (drop si au moins 1 NA)",
            "Une colonne",
            "Plusieurs colonnes",
        ],
        horizontal=False,
    )

    cols_all = list(df.columns)
    col_one: Optional[str] = None
    cols_many: list[str] = []

    if missing_scope == "Une colonne":
        col_one = st.selectbox("Choisir la colonne", cols_all)

    if missing_scope == "Plusieurs colonnes":
        cols_many = st.multiselect("Choisir les colonnes", cols_all)

    m1, m2 = st.columns(2)

    with m1:
        if st.button("🧽 Supprimer les lignes manquantes", use_container_width=True):
            before = len(df)

            if missing_scope == "N'importe quelle colonne (drop si au moins 1 NA)":
                df = df.dropna(axis=0, how="any")

            elif missing_scope == "Une colonne" and col_one:
                df = df.dropna(subset=[col_one], how="any")

            elif missing_scope == "Plusieurs colonnes" and cols_many:
                df = df.dropna(subset=cols_many, how="any")

            st.session_state["df"] = df
            removed = before - len(df)
            st.success(f"✅ {removed} ligne(s) supprimée(s)")
            st.rerun()

    with m2:
        if st.button("📊 Compter les valeurs manquantes", use_container_width=True):
            na_counts = df.isna().sum().sort_values(ascending=False)
            st.dataframe(na_counts.to_frame(name="NA").T)


# === Commande en langage naturel (API) ===
user_input = st.text_input(
    "Que veux-tu faire avec ce fichier ?",
    placeholder="Ex : Supprime les lignes où la colonne 'age' est manquante",
)


if user_input and client:
    prompt = f"""
Tu es un assistant Python expert en nettoyage de données avec pandas.
Voici un DataFrame nommé df.
L'utilisateur demande : \"{user_input}\"

Contraintes:
- Retourne uniquement du code Python exécutable.
- Le code doit MODIFIER le DataFrame df (in-place ou par réassignation), et laisser df comme résultat final.
- N'utilise pas d'import.
- N'accède pas au système de fichiers.
- N'utilise pas de réseau.
""".strip()

    result_container = st.empty()

    # Je n'affiche pas le code pendant le chargement
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

        # Rien n'est visible par défaut ; le code est caché derrière un expander avec emoji
        with st.expander("🧠 Voir le code généré", expanded=False):
            st.code(code, language="python")

        if st.button("▶️ Exécuter ce nettoyage"):
            try:
                local_vars = {"df": df.copy()}
                exec(code, {}, local_vars)

                if "df" not in local_vars:
                    raise RuntimeError("Le code généré n'a pas laissé de variable 'df' en sortie.")

                df = local_vars["df"]
                st.session_state["df"] = df
                result_container.success("✅ Nettoyage appliqué avec succès !")
                result_container.dataframe(df.head())

            except Exception as e:
                result_container.error(f"❌ Erreur pendant l'exécution du code : {e}")


# === Codebook PDF (automatique) ===
with st.expander("📄 Codebook (PDF)", expanded=False):
    st.caption("Génère automatiquement un codebook (dictionnaire des variables) à partir du dataset courant.")

    default_name = st.session_state.get("uploaded_name") or "dataset"
    pdf_title = st.text_input("Titre du codebook", value=f"Codebook - {default_name}")

    if st.button("📄 Générer le PDF", use_container_width=True):
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
            st.error(f"Erreur lors de la génération du PDF : {e}")


# === Téléchargement ===
df = st.session_state.get("df", df)
cleaned_csv = df.to_csv(index=False).encode("utf-8")

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

    # Test CSV
    fake_csv = _FakeUpload("test.csv", b"a,b\n1,2\n")
    df_csv, t_csv = load_data(fake_csv)
    assert t_csv == "csv" and df_csv.shape == (1, 2)

    # Test JSON records
    payload = json.dumps([{"a": 1, "b": 2}]).encode("utf-8")
    fake_json = _FakeUpload("test.json", payload)
    df_json, t_json = load_data(fake_json)
    assert t_json == "json" and df_json.shape == (1, 2)

    # Test JSON Lines
    payload_jsonl = b"{\"a\": 1, \"b\": 2}\n{\"a\": 3, \"b\": 4}\n"
    fake_jsonl = _FakeUpload("test.json", payload_jsonl)
    df_jsonl, t_jsonl = load_data(fake_jsonl)
    assert t_jsonl == "json" and df_jsonl.shape == (2, 2)

    # Test standardisation : styles + acronymes
    assert _standardize_text_value("  chu  bruxelles ", True, {"CHU"}, "Majuscule à chaque mot") == "CHU Bruxelles"
    assert _standardize_text_value("abc DEF", False, set(), "Tout en minuscules") == "abc def"
    assert _standardize_text_value("abc DEF", False, set(), "Tout en MAJUSCULES") == "ABC DEF"
    assert _standardize_text_value("abc DEF", False, set(), "Commencer par une majuscule") == "Abc def"

    st.success("✅ DATACURE_RUN_TESTS: tous les mini-tests ont réussi")

