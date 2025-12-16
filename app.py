# datacure_cleaning_app.py
# -----------------------------------------------------------------------------
# Je construis ici un prototype Streamlit “Data2C / Datacure” :
# 1) j’importe un fichier (CSV / Excel / JSON / Stata)
# 2) je décris en langage naturel un nettoyage à effectuer
# 3) je demande à l’API OpenAI de générer du code pandas
# 4) j’exécute ce code sur une copie du DataFrame
# 5) je propose le téléchargement du résultat
# -----------------------------------------------------------------------------

from __future__ import annotations

import io
import os
from typing import Tuple, Optional

import pandas as pd
import streamlit as st
from openai import OpenAI


# === Configuration Streamlit ===
# Je configure la page et je pose le titre.
st.set_page_config(page_title="Datacure Prototype", layout="wide")
st.title("Datacure - Assistant de nettoyage de données (v0)")


# === Chargement de la clé OpenAI ===
# Je récupère la clé depuis Streamlit secrets (prod) ou une variable d’environnement (dev).
api_key = st.secrets.get("OPENAI_API_KEY") if hasattr(st, "secrets") else None
api_key = api_key or os.getenv("OPENAI_API_KEY")

# J’instancie le client uniquement si j’ai une clé valide.
client: Optional[OpenAI] = None
if not api_key:
    st.warning(
        "⚠️ Clé API OpenAI manquante. Configure-la dans .streamlit/secrets.toml "
        "ou comme variable d'environnement (OPENAI_API_KEY)."
    )
else:
    client = OpenAI(api_key=api_key)


# === Upload fichier multi-formats ===
# J’accepte CSV, Excel, JSON et Stata.
uploaded_file = st.file_uploader(
    "Charge un fichier de données",
    type=["csv", "xlsx", "xls", "json", "dta"],
)


def load_data(file) -> Tuple[pd.DataFrame, str]:
    """Je charge un fichier Streamlit en DataFrame pandas.

    Je retourne : (df, file_type)
    - file_type ∈ {"csv", "excel", "json", "stata"}

    Notes:
    - Pour Excel, je laisse la possibilité de choisir une feuille.
    - Pour JSON, je tente d’abord une lecture standard, puis JSON Lines si besoin.
    """

    filename = (getattr(file, "name", "") or "").lower().strip()

    # --- CSV ---
    if filename.endswith(".csv"):
        # Je lis le CSV tel quel.
        df = pd.read_csv(file)
        return df, "csv"

    # --- Excel ---
    if filename.endswith((".xls", ".xlsx")):
        # Je charge le classeur et je propose à l’utilisateur de choisir la feuille.
        xls = pd.ExcelFile(file)
        # Par défaut, je sélectionne automatiquement la première feuille (index=0)
        sheet = st.selectbox("Choisis une feuille Excel", xls.sheet_names, index=0)
        df = pd.read_excel(xls, sheet_name=sheet)
        return df, "excel"

    # --- JSON ---
    if filename.endswith(".json"):
        # Je tente une lecture JSON standard.
        try:
            df = pd.read_json(file)
            return df, "json"
        except ValueError:
            # Si ça échoue (souvent le cas pour JSON Lines), je réessaie en lines=True.
            try:
                file.seek(0)
            except Exception:
                pass
            df = pd.read_json(file, lines=True)
            return df, "json"

    # --- Stata (.dta) ---
    if filename.endswith(".dta"):
        # Je lis le fichier Stata.
        df = pd.read_stata(file)
        return df, "stata"

    # Si le format n’est pas supporté, je lève une erreur claire.
    raise ValueError("Format de fichier non supporté. Utilise CSV, Excel, JSON ou Stata (.dta).")


# === UX : si aucun fichier n’est chargé ===
if not uploaded_file:
    st.info("📂 Veuillez charger un fichier (CSV, Excel, JSON ou Stata) pour commencer.")
    st.stop()


# === Lecture du fichier ===
try:
    df, file_type = load_data(uploaded_file)
    st.subheader("Aperçu du fichier")
    st.caption(f"📄 Format détecté : {file_type}")
    st.dataframe(df.head())
except Exception as e:
    st.error(f"Erreur de lecture du fichier : {e}")
    st.stop()


# === Commande en langage naturel ===
user_input = st.text_input(
    "Que veux-tu faire avec ce fichier ?",
    placeholder="Ex : Supprime les lignes où la colonne 'age' est manquante",
)


# === Appel OpenAI (génération de code) ===
# Je n’appelle l’API que si l’utilisateur a écrit une instruction et que j’ai un client.
if user_input and client:
    # Je demande explicitement à GPT de renvoyer du code qui modifie df.
    # IMPORTANT : en prod, exécuter du code généré est risqué. Ici c’est volontairement prototype.
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

    with st.expander("🔍 Voir le prompt envoyé", expanded=False):
        st.code(prompt)

    # J'utilise des placeholders pour éviter d'afficher du code pendant les phases de chargement/rerun
    code_container = st.empty()
    result_container = st.empty()

    with st.spinner("🧠 Génération du code Python par GPT..."):
        try:
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )

            # Je stocke le code généré dans le session_state
            st.session_state["generated_code"] = response.choices[0].message.content.strip()

        except Exception as e:
            st.error(f"❌ Erreur lors de l'appel à l'API OpenAI : {e}")

    # Une fois le chargement terminé, je propose de voir le code SANS l'afficher par défaut
    if "generated_code" in st.session_state:
        code = st.session_state["generated_code"]

        # L'utilisateur voit uniquement une action avec un emoji ; aucun code n'est visible par défaut
        with st.expander("🧠 Voir le code généré", expanded=False):
            st.code(code, language="python")

        if st.button("▶️ Exécuter ce nettoyage"):
            try:
                local_vars = {"df": df.copy()}
                exec(code, {}, local_vars)

                if "df" not in local_vars:
                    raise RuntimeError("Le code généré n'a pas laissé de variable 'df' en sortie.")

                df = local_vars["df"]
                result_container.success("✅ Nettoyage appliqué avec succès !")
                result_container.dataframe(df.head())

            except Exception as e:
                result_container.error(f"❌ Erreur pendant l'exécution du code : {e}")


# === Téléchargement (CSV par défaut) ===
# Je propose toujours un export CSV (interopérable partout).
cleaned_csv = df.to_csv(index=False).encode("utf-8")

st.download_button(
    label="📥 Télécharger le fichier nettoyé (CSV)",
    data=cleaned_csv,
    file_name="fichier_nettoye.csv",
    mime="text/csv",
)


# === (Option) Exports alternatifs ===
# Si je veux activer un export Stata, je peux décommenter ce bloc.
# Exemple Stata (attention: peut échouer si colonnes non compatibles avec Stata):
#
# if file_type == "stata":
#     buf = io.BytesIO()
#     df.to_stata(buf, write_index=False)
#     st.download_button(
#         label="📥 Télécharger le fichier nettoyé (.dta)",
#         data=buf.getvalue(),
#         file_name="fichier_nettoye.dta",
#         mime="application/octet-stream",
#     )


# === Mini-tests (optionnels) ===
# Je n’exécute ces tests que si je pose la variable d’environnement DATACURE_RUN_TESTS=1.
# Ça me permet de valider rapidement la fonction load_data sans perturber Streamlit.
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
            # pandas lit comme un file-like, donc je délègue vers BytesIO
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

    st.success("✅ DATACURE_RUN_TESTS: tous les mini-tests ont réussi")

