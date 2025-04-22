from datetime import datetime
from airflow import DAG
from airflow.operators.python import PythonOperator
import pandas as pd
import psycopg2
from pymongo import MongoClient
import os
from airflow.utils.log.logging_mixin import LoggingMixin

log = LoggingMixin().log

DATABASE_URL = os.getenv("DATABASE_URL")
MONGO_URL = os.getenv("MONGO_URL")

if not DATABASE_URL or not MONGO_URL:
    raise ValueError("DATABASE_URL et MONGO_URL doivent être définis dans les variables d'environnement.")

# Répertoire temporaire compatible WSL2
TMP_DIR = "/tmp" if os.name != 'nt' else os.path.join(os.getenv('TEMP', 'temp'), 'airflow_tmp')
os.makedirs(TMP_DIR, exist_ok=True)

# Fonction pour extraire les données de Neon
def extract_data():
    try:
        with psycopg2.connect(DATABASE_URL) as conn:
            queries = {
                'trans': "SELECT * FROM trans",
                'loan': "SELECT * FROM loan",
                'account': "SELECT * FROM account",
                'district': "SELECT * FROM district"
            }
            data = {}
            for table, query in queries.items():
                log.info(f"Extraction des données de la table {table}...")
                df = pd.read_sql(query, conn)
                if df.empty:
                    log.warning(f"La table {table} est vide.")
                data[table] = df
            for table, df in data.items():
                df.to_csv(os.path.join(TMP_DIR, f"{table}.csv"), index=False)
        log.info("Extraction des données terminée.")
    except Exception as e:
        log.error(f"Erreur lors de l'extraction des données : {str(e)}")
        raise

# Fonction pour parser les dates avec gestion des erreurs
def parse_date(date_str):
    try:
        if isinstance(date_str, str) and "-" in date_str:
            return pd.to_datetime(date_str, format="%Y-%m-%d")
        return pd.to_datetime(date_str, format="%y%m%d")
    except (ValueError, TypeError) as e:
        log.warning(f"Format de date invalide : {date_str}. Erreur : {str(e)}")
        return None

# Fonction pour calculer les KPI
def compute_kpis():
    try:
        # Charger les données extraites
        log.info("Chargement des fichiers CSV pour le calcul des KPI...")
        df_transaction = pd.read_csv(os.path.join(TMP_DIR, "trans.csv"))
        df_loan = pd.read_csv(os.path.join(TMP_DIR, "loan.csv"))
        df_account = pd.read_csv(os.path.join(TMP_DIR, "account.csv"))
        df_district = pd.read_csv(os.path.join(TMP_DIR, "district.csv"))

        # Conversion des dates
        df_transaction["date"] = df_transaction["date"].apply(parse_date)
        df_loan["date"] = df_loan["date"].apply(parse_date)
        # Supprimer les lignes avec des dates invalides
        df_transaction = df_transaction.dropna(subset=["date"])
        df_loan = df_loan.dropna(subset=["date"])

        # KPI 1 : Volume de transactions par mois
        volume_transaction_count = df_transaction.groupby(df_transaction["date"].dt.to_period("M"))["trans_id"].count().reset_index()
        volume_transaction_count.columns = ["date", "volume"]
        volume_transaction_amount = df_transaction.groupby(df_transaction["date"].dt.to_period("M"))["amount"].sum().reset_index()
        volume_transaction_amount.columns = ["date", "ca"]
        transaction_value_count_per_mounth_df = pd.merge(volume_transaction_count, volume_transaction_amount, on="date", how="inner")

        # KPI 2 : Opérations sur transactions dominantes
        transaction_operation = pd.DataFrame(
            df_transaction["operation"].value_counts(normalize=True).reset_index()
        )
        transaction_operation.columns = ["operation", "proportion"]

        # KPI 3 : Nombre et montant moyen des transactions par compte
        transaction_stats = df_transaction.groupby("account_id").agg(
            transaction_count=("trans_id", "count"),
            transaction_value_count=("amount", "mean")
        ).reset_index()
        transaction_value_count_per_count_df = transaction_stats.sort_values("transaction_count", ascending=False)

        # KPI 4 : Zones les plus actives en termes de nombre de transactions
        df_account_district = pd.merge(transaction_stats[["account_id", "transaction_count"]], df_account[["account_id", "district_id"]], on="account_id", how="inner")
        dominant_district_count_df = df_account_district.groupby("district_id")["transaction_count"].sum().sort_values(ascending=False).reset_index()
        df_district.rename(columns={"A1": "district_id"}, inplace=True)
        dominant_district_count_df = pd.merge(dominant_district_count_df, df_district[["district_id", "A2"]], on="district_id", how="inner")
        dominant_district_count_df = dominant_district_count_df.rename(columns={"A2": "district_name"})
        dominant_district_count_df["transaction_count"] = dominant_district_count_df["transaction_count"] / dominant_district_count_df["transaction_count"].sum()

        # KPI 5 : Prêts par district
        dominant_district_loan_count = df_loan.groupby("account_id")["loan_id"].count().reset_index()
        dominant_district_loan_count = pd.merge(df_account[["account_id", "district_id"]], dominant_district_loan_count, on="account_id", how="inner")
        dominant_district_loan_count = dominant_district_loan_count.groupby("district_id")["loan_id"].sum().reset_index()
        dominant_district_loan_count = dominant_district_loan_count.sort_values("loan_id", ascending=False)
        dominant_district_loan_count = pd.merge(dominant_district_loan_count, df_district[["district_id", "A2"]], on="district_id", how="inner")
        dominant_district_loan_count.rename(columns={"loan_id": "loan_count", "A2": "district_name"}, inplace=True)

        # KPI 6 : Statuts des prêts
        loan_status_df = pd.DataFrame(df_loan["status"].value_counts(normalize=True)).reset_index()
        loan_status_df.columns = ["status", "proportion"]

        # Sauvegarder les KPI
        kpi_outputs = {
            'transaction_value_count_per_mounth_df.csv': transaction_value_count_per_mounth_df,
            'transaction_operation.csv': transaction_operation,
            'transaction_value_count_per_count_df.csv': transaction_value_count_per_count_df,
            'dominant_district_count_df.csv': dominant_district_count_df,
            'dominant_district_loan_count.csv': dominant_district_loan_count,
            'loan_status_df.csv': loan_status_df,
        }
        for file, df in kpi_outputs.items():
            df.to_csv(os.path.join(TMP_DIR, file), index=False)
        log.info("Calcul des KPI terminé.")
    except Exception as e:
        log.error(f"Erreur lors du calcul des KPI : {str(e)}")
        raise

# Fonction pour charger les KPI dans MongoDB
def load_to_mongodb():
    try:
        with MongoClient(MONGO_URL) as client:
            db = client["payment_kpi_db"]
            kpi_files = {
                'transaction_value_count_per_mounth_df.csv': 'transaction_value_count_per_mounth_df',
                'transaction_operation.csv': 'transaction_operation',
                'transaction_value_count_per_count_df.csv': 'transaction_value_count_per_count_df',
                'dominant_district_count_df.csv': 'dominant_district_count_df',
                'dominant_district_loan_count.csv': 'dominant_district_loan_count',
                'loan_status_df.csv': 'loan_status_df',
            }
            for file, collection_name in kpi_files.items():
                log.info(f"Chargement de {file} dans la collection {collection_name}...")
                df = pd.read_csv(os.path.join(TMP_DIR, file))
                data = df.to_dict('records')
                collection = db[collection_name]
                collection.delete_many({})
                collection.insert_many(data)
            log.info("Chargement dans MongoDB terminé.")
    except Exception as e:
        log.error(f"Erreur lors du chargement dans MongoDB : {str(e)}")
        raise

# Définir le DAG
default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
}

with DAG(
    'payment_kpi_pipeline',
    default_args=default_args,
    description='Pipeline pour calculer et charger les KPI bancaires dans MongoDB',
    schedule_interval='@monthly',
    start_date=datetime(2025, 4, 22),
    catchup=False,
) as dag:

    extract_task = PythonOperator(
        task_id='extract_data',
        python_callable=extract_data,
    )

    compute_task = PythonOperator(
        task_id='compute_kpis',
        python_callable=compute_kpis,
    )

    load_task = PythonOperator(
        task_id='load_to_mongodb',
        python_callable=load_to_mongodb,
    )

    extract_task >> compute_task >> load_task