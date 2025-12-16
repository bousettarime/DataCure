# datacure_cleaning_app.py
# -----------------------------------------------------------------------------
# Je construis ici un prototype Streamlit “Data2C / Datacure” :
# 1) j’importe un fichier (CSV / Excel / JSON / Stata)
# 2) je décris en langage naturel un nettoyage à effectuer
# 3) je demande à l’API OpenAI de générer du code pandas
# 4) j’exécute ce code sur une copie du DataFrame
# 5) je propose le téléchargement du résultat
# -----------------------------------------------------------------------------

import os
import io
import streamlit as st
import pandas as pd
from openai import OpenAI


# === Configuration Streamlit ===
st.set_page_config(page_title="Datacure Prototype", layout="wide")
st.title("Datacure - Assistant de nettoyage de données (v0)")


# === Chargement de la clé OpenAI ===
# Je récupère la clé depuis Streamlit secrets (prod) ou une variable d’environnement (dev).
api_key = st.secrets.get("OPENAI_API_KEY") if hasattr(st, "secrets") else None
api_key = api_key or os.getenv("OPENAI_API_KEY")

# J’instancie le client uniquement si j’ai une clé valide.
client = None
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


def load_data(file: "st.runtime.uploaded_file_manager.UploadedFile") -> tuple[pd.DataFrame, str]:
    """Je charge un fichier Streamlit en DataFrame pandas.

    Je retourne : (df, file_type)
    - file_type ∈ {"csv", "excel", "json", "stata"}

    Notes:
    - Pour Excel, je laisse la possibilité de choisir une feuille.
    - Pour JSON, je tente d’abord une lecture standard, puis une lecture JSON Lines si besoin.
    """

    filename = (file.name or "").lower().strip()

    # --- CSV ---
    if filename.endswith(".csv"):
        # Je lis le CSV tel quel (pandas gère automatiquement la plupart des séparateurs standards,
        # mais si tu as beaucoup de fichiers avec ;, on pourra ajouter un détecteur de sep).
        df = pd.read_csv(file)
        return df, "csv"

    # --- Excel ---
    if filename.endswith((".xls", ".xlsx")):
        # Je charge le classeur et je propose à l’utilisateur de choisir la feuille.
        xls = pd.ExcelFile(file)
        sheet = st.selectbox("Choisis une feuille Excel", xls.sheet_names)
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
            # Je dois remettre le curseur au début, sinon pandas lit “vide”.
            file.seek(0)
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

    with st.spinner("🧠 Génération du code Python par GPT..."):
        try:
            # Note: tu peux remplacer gpt-3.5-turbo par un modèle plus récent.
            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )

            code = response.choices[0].message.content.strip()

            st.subheader("💡 Code généré")
            st.code(code, language="python")

            # Bouton d’exécution
            if st.button("▶️ Exécuter ce code sur le DataFrame"):
                try:
                    # J’exécute sur une copie pour éviter de casser df si le code plante.
                    local_vars = {"df": df.copy()}

                    # Je fournis un namespace global vide ({}), et un locals contrôlé.
                    # Attention : cela n’est pas une sandbox de sécurité.
                    exec(code, {}, local_vars)

                    # Je récupère df modifié.
                    if "df" not in local_vars:
                        raise RuntimeError("Le code généré n'a pas laissé de variable 'df' en sortie.")

                    df = local_vars["df"]

                    st.success("✅ Nettoyage appliqué avec succès !")
                    st.dataframe(df.head())

                except Exception as e:
                    st.error(f"❌ Erreur pendant l'exécution du code : {e}")

        except Exception as e:
            st.error(f"❌ Erreur lors de l'appel à l'API OpenAI : {e}")


# === Téléchargement (CSV par défaut) ===
# Je propose toujours un export CSV (interopérable partout).
# Si tu veux, je peux aussi ajouter des exports conditionnels Excel/Stata.
cleaned_csv = df.to_csv(index=False).encode("utf-8")

st.download_button(
    label="📥 Télécharger le fichier nettoyé (CSV)",
    data=cleaned_csv,
    file_name="fichier_nettoye.csv",
    mime="text/csv",
)


# === (Option) Exports alternatifs ===
# Si tu veux activer un export Stata/Excel, je peux te l’ajouter proprement ici.
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
"}


