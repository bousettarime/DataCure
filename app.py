# datacure_cleaning_app.py
# -----------------------------------------------------------------------------
# Je construis ici un prototype Streamlit “Data2C / Datacure” :
# - J'importe un fichier (CSV / Excel / JSON / Stata)
# - Je propose un bouton “Standardiser le texte” (tout / colonne / ligne)
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


# === Configuration Streamlit ===
st.set_page_config(page_title="Datacure Prototype", layout="wide")
st.title("Datacure - Assistant de nettoyage de données (v0)")


# === Clé OpenAI ===
api_key = st.secrets.get("OPENAI_API_KEY") if hasattr(st, "secrets") else None
api_key = api_key or os.getenv("OPENAI_API_KEY")

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


def _standardize_text_value(x, remove_accents: bool, acronyms: set[str]) -> object:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return x
    if not isinstance(x, str):
        return x

    s = x.strip()
    if not s:
        return s

    if remove_accents:
        s = _remove_accents(s)

    s = " ".join(s.split())
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

# Source de vérité
df = st.session_state["df"]
file_type = st.session_state.get("file_type", file_type_in)


# === Aperçu ===
st.subheader("Aperçu du fichier")
st.caption(f"📄 Format détecté : {file_type}")
st.dataframe(df.head())


# === Standardiser le texte (sans API) ===
with st.expander("🧹 Standardiser le texte", expanded=False):
    cols_text = _text_columns(df)

    remove_acc = st.checkbox("Supprimer les accents", value=True)
    acronyms_raw = st.text_input(
        "Acronymes à garder en MAJ (séparés par des virgules)",
        value="",
    )
    acronyms = {a.strip().upper() for a in acronyms_raw.split(",") if a.strip()}

    scope = st.radio(
        "Appliquer sur",
        ["Tout le tableau", "Une colonne", "Une ligne"],
        horizontal=True,
    )

    selected_col: Optional[str] = None
    if scope == "Une colonne":
        if cols_text:
            selected_col = st.selectbox("Colonne", cols_text)
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
            )
        )

    c1, c2, c3 = st.columns(3)

    with c1:
        if st.button("✨ Standardiser", use_container_width=True):
            if not cols_text:
                st.warning("Je n'ai trouvé aucune colonne texte à standardiser.")
            else:
                if scope == "Tout le tableau":
                    for c in cols_text:
                        df[c] = df[c].apply(
                            lambda v: _standardize_text_value(v, remove_acc, acronyms)
                        )

                elif scope == "Une colonne" and selected_col:
                    df[selected_col] = df[selected_col].apply(
                        lambda v: _standardize_text_value(v, remove_acc, acronyms)
                    )

                elif scope == "Une ligne" and selected_row is not None:
                    r = selected_row
                    for c in cols_text:
                        df.at[df.index[r], c] = _standardize_text_value(
                            df.at[df.index[r], c], remove_acc, acronyms
                        )

                st.session_state["df"] = df
                st.success("✅ Standardisation appliquée")
                st.rerun()

    with c2:
        if st.button("↩️ Annuler les changements", use_container_width=True):
            _reset_to_uploaded_file()
            st.success("✅ Réinitialisé")
            st.rerun()

    with c3:
        if st.button("👀 Voir un aperçu", use_container_width=True):
            st.dataframe(st.session_state["df"].head())


# === Commandes rapides (sans API) ===
with st.expander("⚡ Commandes rapides", expanded=False):
    st.caption("Actions one-click pour nettoyer sans passer par l'API")

    missing_scope = st.radio(
        "Supprimer les lignes avec valeurs manquantes",
        ["N'importe quelle colonne (drop si au moins 1 NA)", "Une colonne", "Plusieurs colonnes"],
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

    st.success("✅ DATACURE_RUN_TESTS: tous les mini-tests ont réussi")
