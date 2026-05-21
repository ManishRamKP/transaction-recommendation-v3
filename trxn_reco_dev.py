import os
from flask import Flask, request, jsonify
import uuid
import json
import time
from datetime import datetime
import pandas as pd
import boto3
import requests
from sqlalchemy import create_engine

app = Flask(__name__)

# Load configuration from a JSON file
with open("config_dev.json", "r") as config_file:
    config_data = json.load(config_file)
# S3 Bucket Configurations
S3_BUCKET_NAME = os.environ.get('S3_AUDIT_BUCKET', 'YOUR_S3_AUDIT_BUCKET')
S3_FOLDER = 'transaction_recommendation'
# AWS Configurations
AWS_ACCESS_KEY_ID = config_data["aws"]["aws_access_key_id"]
AWS_SECRET_ACCESS_KEY = config_data["aws"]["aws_secret_access_key"]
REGION_NAME = config_data["aws"]["region_name"]
ATHENA_S3_OUTPUT = config_data["athena"]["s3_output_location"]

# PostgreSQL Configurations
POSTGRES_DB_prod = config_data["postgres_dev"]["db"]
POSTGRES_USER_prod= config_data["postgres_dev"]["user"]
POSTGRES_PASSWORD_prod = config_data["postgres_dev"]["password"]
POSTGRES_HOST_prod = config_data["postgres_dev"]["host"]
POSTGRES_PORT_prod = config_data["postgres_dev"]["port"]

# PostgreSQL Configurations
POSTGRES_DB_dev = config_data["postgres_dev"]["db"]
POSTGRES_USER_dev = config_data["postgres_dev"]["user"]
POSTGRES_PASSWORD_dev = config_data["postgres_dev"]["password"]
POSTGRES_HOST_dev = config_data["postgres_dev"]["host"]
POSTGRES_PORT_dev = config_data["postgres_dev"]["port"]

# Initialize a boto3 session
session = boto3.Session(
    aws_access_key_id=AWS_ACCESS_KEY_ID,
    aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
    region_name=REGION_NAME
)
s3_client = session.client('s3')
# Function to fetch invoices from PostgreSQL instead of Athena
def run_trade_query(borrower_gst):
    # Read 'inv_delayin_days' from config_data
    inv_delayin_days = config_data.get("inv_delayin_days", 30)

    # Connect to PostgreSQL
    engine = create_engine(f'postgresql://{POSTGRES_USER_prod}:{POSTGRES_PASSWORD_prod}@{POSTGRES_HOST_prod}:{POSTGRES_PORT_prod}/{POSTGRES_DB_prod}')
    query = f"""
   WITH ranked_trades AS (
    SELECT
        tm.id AS trademaster_id,
        tm.trade_ref_no,
        tm.trade_id AS trademaster_trade_id,
        tm.trade_date AS invoice_date,
        tm.source_val AS ptgstin,
        tm.target_val AS ctin,
        tm.trade_no AS invoice_id,
        tm.trade_amount AS invoice_amount,
        tm.target_name AS lgl_nm,
        tm.trade_source,
        ROW_NUMBER() OVER (
            PARTITION BY tm.trade_no
            ORDER BY
                CASE
                    WHEN tm.trade_source = 'TALLY' THEN 1
                    WHEN tm.trade_source = 'GDC' THEN 2
                    WHEN tm.trade_source = 'GST' THEN 3

                    ELSE 4
                END
        ) AS rn
    FROM 
        cbapis.trade_master tm
    WHERE 
        tm.source_val = '{borrower_gst}'
        AND tm.erp_payment_date IS NULL
        AND tm.trade_source IN ('TALLY', 'GDC','GST')
        AND tm.trade_date >= CURRENT_DATE - INTERVAL '30 days'
)
SELECT
    trademaster_id,
    trade_ref_no,
    trademaster_trade_id,
    invoice_date,
    ptgstin,
    ctin,
    invoice_id,
    invoice_amount,
    lgl_nm,
    trade_source
FROM
    ranked_trades
WHERE
    rn = 1;"""

    df = pd.read_sql_query(query, engine)
    engine.dispose()
    return df

# Function to run additional queries from PostgreSQL
def run_postgres_queries(borrower_gst):
    engine = create_engine(f'postgresql://{POSTGRES_USER_dev}:{POSTGRES_PASSWORD_dev}@{POSTGRES_HOST_dev}:{POSTGRES_PORT_dev}/{POSTGRES_DB_dev}')
    
    query1 = f"""
    SELECT 
    t.*, fr.id, fr.request_date, fr.request_status 
FROM 
   cbapis.trade_master AS t
INNER JOIN 
    cbapis.finance_request_trade_map AS frtm 
ON 
    t.id = frtm.trademaster_id 
INNER JOIN 
    cbapis.finance_request AS fr 
ON 
    frtm.financerequest_id  = fr.id
WHERE 
    t.source_val  = '{borrower_gst}';
    """
    
    query2 = f"""
    SELECT
ia.*
FROM
cbapis.individual_assessment AS ia
INNER JOIN
cbapis.cbcre_process AS cp
ON
ia.cbcreprocess_id = cp.id
INNER JOIN
cbapis.finance_request AS fr
ON
cp.financerequest_id = fr.id
INNER JOIN
cbapis.anchor_trader AS at
ON
fr.anchortrader_id = at.id
WHERE
at.anchor_trader_gst = '{borrower_gst}'
AND cp.status = 'INPROCESS_LATEST'
AND fr.request_status = 'FRUR';
    """
    
    df_query1 = pd.read_sql_query(query1, engine)
    df_query2 = pd.read_sql_query(query2, engine)
    
    engine.dispose()
    return df_query1, df_query2

def compute_credit_score(borrower_gst, trader_gst, current_invoice_amount, ctin, invoice_id):
    api_url = "os.environ.get('CRE_API_URL', 'YOUR_CRE_API_URL')"
    headers = { 'Authorization': 'Bearer ' + os.environ.get('API_BEARER_TOKEN', 'YOUR_BEARER_TOKEN')}
    additional_metrics = {
        "grn_present": False,
        "e_invoice_present": False,
        "e_way_bill_present": False,
        "trader_partner_confirmation": False
    }
    data = {
        "invoice_data": {
            "borrower_gst": borrower_gst,
            "trader_gst": trader_gst,
            "current_invoice_amount": current_invoice_amount
        },
        "additional_metrics": additional_metrics
    }
    try:

        response = requests.post(api_url, json=data, headers=headers)
        if response.status_code == 200:
            result = response.json()
            return {
                "success": True,
                "base_score": result.get('base_score', 45.5),
                "credit_score": result.get('credit_score', 45.5),
                "request_id": result.get('request_id'),
                "timestamp": result.get('timestamp'),
                "highlights": result.get('highlights', []),
                "observations": result.get('observations', []),
                "final_verdict": result.get('final_verdict', "Insufficient data for verdict")
            }
        else:
            print(f"No data for ctin {ctin} and invoice_id {invoice_id}: {response.status_code}")
            return {"success": False}
    except requests.exceptions.RequestException as e:
        print(f"Request failed for ctin {ctin} and invoice_id {invoice_id}: {e}")
        return {"success": False}

# Function to calculate cumulative trade score
def calculate_cumulative_trade_score(df):
    if not df.empty and 'credit_score' in df.columns:
        df['credit_score'] = pd.to_numeric(df['credit_score'], errors='coerce')
        df['invoice_amount'] = pd.to_numeric(df['invoice_amount'], errors='coerce')
        return (df['credit_score'] * df['invoice_amount']).sum() / df['invoice_amount'].sum()
    return 45.5

@app.route('/transaction-recommendation', methods=['POST'])
def transaction_recommendation():
    data = request.json
    if not data:
        return jsonify({"error": "Request payload is missing."}), 400

    # Fetch 'total_amount' and 'borrower_gst' from request data
    total_amount_requested = data.get('total_amount')
    borrower_gst = data.get('borrower_gst')

    # Check for the presence of 'total_amount' and 'borrower_gst'
    if total_amount_requested is None or borrower_gst is None:
        return jsonify({"error": "Missing 'total_amount' or 'borrower_gst'."}), 400

    # Attempt to convert 'total_amount' to a float
    try:
        total_amount_requested = float(total_amount_requested)
    except ValueError:
        return jsonify({"error": "'total_amount' must be a number."}), 400
    
    # Execute Athena query and process data
    try:
        df = run_trade_query(borrower_gst)
        if df.empty:
            return jsonify({"error": "No data returned from query."}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to execute trademaster query: {str(e)}"}), 500

    # Check if 'borrower_gst' is a non-empty string
    if not isinstance(borrower_gst, str) or not borrower_gst.strip():
        return jsonify({"error": "'borrower_gst' must be a non-empty string."}), 400

    # Execute Athena query and process data
    try:
        df = run_trade_query(borrower_gst)
        if df.empty:
            return jsonify({"error": "No data returned from query."}), 400
    except Exception as e:
        return jsonify({"error": f"Failed to execute Athena query: {str(e)}"}), 500

    # Run additional queries and process data
    try:
        query1_df, query2_df = run_postgres_queries(borrower_gst)
        
        # If the additional queries return no data, log a message and continue
        if query1_df.empty:
            print("No data returned from query1.")
            query1_df = pd.DataFrame()  # Create an empty DataFrame to avoid KeyError
        if query2_df.empty:
            print("No data returned from query2.")
            query2_df = pd.DataFrame()  # Create an empty DataFrame to avoid KeyError
    except Exception as e:
        return jsonify({"error": f"Failed to execute additional queries: {str(e)}"}), 500

    # Prepare the data
    df['invoice_amount'] = pd.to_numeric(df['invoice_amount'], errors='coerce').abs()
    df['invoice_date'] = pd.to_datetime(df['invoice_date'])
    df = df.sort_values(by=['invoice_date', 'invoice_amount'], ascending=[True, True])

    # Rename 'invoice_number' to 'invoice_id' in query1_df for consistency if it's not empty
    if not query1_df.empty:
        if 'trade_no' in query1_df.columns:
            query1_df.rename(columns={'trade_no': 'invoice_id'}, inplace=True)

    # Define statuses to exclude
    statuses_to_exclude = ['FRPR', 'FRCD']

    # Exclude invoices based on additional queries (only if they contain the necessary data)
    excluded_invoices_query1 = set()
    excluded_invoices_query2 = set()

    # Handle query1_df data if it contains 'request_status' and 'invoice_id'
    if not query1_df.empty and 'request_status' in query1_df.columns and 'invoice_id' in query1_df.columns:
        excluded_invoices_query1 = set(query1_df.loc[query1_df['request_status'].isin(statuses_to_exclude), 'invoice_id'])

    # Handle query2_df data if it contains 'invoice_id'
    if not query2_df.empty and 'invoice_id' in query2_df.columns:
        excluded_invoices_query2 = set(query2_df['invoice_id'])

    
    # Combine the excluded invoices
    excluded_invoices = excluded_invoices_query1.union(excluded_invoices_query2)

    # Check for rejected invoices and their counts (only if query1_df is not empty)
    if not query1_df.empty and 'request_status' in query1_df.columns:
        # Ensure 'invoice_id' exists before grouping
        if 'invoice_id' in query1_df.columns:
            rejected_invoices = query1_df[query1_df['request_status'].isin(statuses_to_exclude)]
            
            # Check if there are any rejected invoices
            if not rejected_invoices.empty:
                rejected_counts = rejected_invoices.groupby('invoice_id').size()
                
                print("Rejected invoices:", rejected_invoices)
                print("Rejected counts:", rejected_counts)
                
                # Add invoices rejected 3 or more times to the excluded set
                for invoice, count in rejected_counts.items():
                    if count >= 3:
                        excluded_invoices.add(invoice)
                
                print("Excluded invoices after rejection check:", excluded_invoices)
            else:
                print("No rejected invoices found.")
        else:
            print("Column 'invoice_id' not found in query1_df")
    else:
        print("query1_df is empty or 'request_status' not in columns")

    df = df[~df['invoice_id'].isin(excluded_invoices)]
    print("Total excluded invoices:", excluded_invoices)


    accumulated_amount = 0.0
    selected_invoices = pd.DataFrame()
    for index, row in df.iterrows():
        invoice_amount = row['invoice_amount']
        if accumulated_amount + invoice_amount <= total_amount_requested:
            credit_details = compute_credit_score(row['ptgstin'], row['ctin'], invoice_amount, row['ctin'], row['invoice_id'])
            if credit_details["success"]:
                if credit_details["credit_score"] >= 35:
                    row['credit_details'] = credit_details
                    row['lgl_nm'] = row.get('lgl_nm')
                    row['trademaster_id'] = row.get('trademaster_id')
                    row['trademaster_trade_id'] = row.get('trademaster_trade_id')
                    selected_invoices = pd.concat([selected_invoices, pd.DataFrame([row]).reset_index(drop=True)], ignore_index=True)
                    accumulated_amount += invoice_amount
                else:
                    print(f"Skipping invoice {row['invoice_id']} with credit score {credit_details['credit_score']}")
        elif accumulated_amount < total_amount_requested and accumulated_amount + invoice_amount <= total_amount_requested * 1.20:
            credit_details = compute_credit_score(row['ptgstin'], row['ctin'], invoice_amount, row['ctin'], row['invoice_id'])
            if credit_details["success"]:
                if credit_details["credit_score"] >= 35:
                    row['credit_details'] = credit_details
                    row['lgl_nm'] = row.get('lgl_nm')
                    row['trademaster_id'] = row.get('trademaster_id')
                    row['trademaster_trade_id'] = row.get('trademaster_trade_id')
                    selected_invoices = pd.concat([selected_invoices, pd.DataFrame([row]).reset_index(drop=True)], ignore_index=True)
                    accumulated_amount += invoice_amount
                else:
                    print(f"Skipping invoice {row['invoice_id']} with credit score {credit_details['credit_score']}")
        if accumulated_amount >= total_amount_requested:
            break  # Stop if we've reached or exceeded the target amount

    # Calculate the cumulative trade score
    if 'credit_details' in selected_invoices.columns:
        selected_invoices['credit_score'] = selected_invoices['credit_details'].apply(lambda x: x['credit_score'])
        selected_invoices.sort_values(by='credit_score', ascending=False, inplace=True)
        cumulative_trade_score = calculate_cumulative_trade_score(selected_invoices)
    else:
        cumulative_trade_score = None

    # Construct the response
    request_id = str(uuid.uuid4())
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cumulative_assessment = {
        "cumulative_trade_score": round(cumulative_trade_score, 2) if cumulative_trade_score is not None else None,
        "request_id": request_id,
        "timestamp": timestamp,
        "total_amount_requested": total_amount_requested,
        "total_invoice_amount": accumulated_amount,
    }

    individual_assessment = []
    for _, row in selected_invoices.iterrows():
        credit_details = row['credit_details']
        assessment_detail = {
            "trade_ref_no": row['trade_ref_no'],
            "trademaster_trade_id":row['trademaster_trade_id'],
            "trademaster_id": row['trademaster_id'],
            "invoice_id": row['invoice_id'],
            "invoice_amount": row['invoice_amount'],
            "invoice_date": row['invoice_date'].strftime("%Y-%m-%d"),
            "ctin": row['ctin'],
            "lgl_nm": row['lgl_nm'],
            "assessment": {
                "request_id": request_id,
                "timestamp": timestamp,
                "base_score": credit_details['base_score'],
                "credit_score": credit_details['credit_score'],
                "highlights": credit_details['highlights'],
                "observations": credit_details['observations'],
                "final_verdict": credit_details['final_verdict']
            }
        }
        individual_assessment.append(assessment_detail)

    response = {
        "cumulative_assessment": cumulative_assessment,
        "individual_assessment": individual_assessment
    }
    print("transaction recommendation:", response)
    # Save the response to S3
    try:
        s3_key = f"{S3_FOLDER}/{request_id}.json"
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=json.dumps(response),
            ContentType='application/json'
        )
        print(f"Response saved to S3 at {S3_BUCKET_NAME}/{s3_key}")
    except Exception as e:
        print(f"Failed to save response to S3: {str(e)}")

    return jsonify(response)


def compute_credit_score_2(invoice_details, additional_metrics):
    api_url = "os.environ.get('CRE_API_URL', 'YOUR_CRE_API_URL')"
    headers = {
        'Authorization': 'Bearer ' + os.environ.get('API_BEARER_TOKEN', 'YOUR_BEARER_TOKEN')  # Make sure to use the actual token here
    }
    # Prepare the payload excluding 'invoice_id' from the 'invoice_data'
    payload = {
        "invoice_data": {
            "borrower_gst": invoice_details.get("borrower_gst"),
            "trader_gst": invoice_details.get("trader_gst"),
            "current_invoice_amount": invoice_details.get("current_invoice_amount")
        },
        "additional_metrics": additional_metrics
    }

    try:
        response = requests.post(api_url, json=payload, headers=headers)
        response.raise_for_status()  # Raises stored HTTPError, if one occurred
        return response.json()
    except requests.RequestException as e:
        print(f"Error calling the credit score API: {e}")
        return None  # Handle error scenario
@app.route('/transaction-recommendation-refine', methods=['POST'])
def transaction_recommendation_refine():
    data = request.json
    trxn_reco_refine = data.get('trxn_reco_refine', [])
    total_invoice_amount = 0
    individual_assessment = []

    for item in trxn_reco_refine:
        # Corrected extraction of 'invoice_details' and 'additional_metrics'
        invoice_details = item.get('invoice_data', {}).get('invoice_details', {})
        additional_metrics = item.get('invoice_data', {}).get('additional_metrics', {})
         # Validate mandatory fields
        required_fields = ["borrower_gst", "trader_gst", "current_invoice_amount", "lgl_nm", "invoice_id", "invoice_date","trademaster_id","trademaster_trade_id","trade_ref_no"]
        missing_fields = [field for field in required_fields if not invoice_details.get(field)]
        if missing_fields:
            return jsonify({
                "error": f"Missing mandatory field(s): {', '.join(missing_fields)}"
            }), 400
        # Fill in missing metrics with False as default
        for metric in ["grn_present", "e_invoice_present", "e_way_bill_present", "trader_partner_confirmation"]:
            additional_metrics.setdefault(metric, False)

        credit_score_data = compute_credit_score_2(invoice_details, additional_metrics)
        if credit_score_data:
            individual_assessment.append({
                "trade_ref_no": invoice_details.get("trade_ref_no"),
                "trademaster_id": invoice_details.get("trademaster_id"),
                "trademaster_trade_id": invoice_details.get("trademaster_trade_id"),
                "invoice_id": invoice_details.get("invoice_id"), 
                "invoice_date": invoice_details.get("invoice_date"), 
                "ctin": invoice_details.get("trader_gst"),
                "lgl_nm": invoice_details.get("lgl_nm"),
                "invoice_amount": invoice_details.get("current_invoice_amount"),
                "assessment": credit_score_data
            })
            total_invoice_amount += invoice_details.get("current_invoice_amount", 0)

    if individual_assessment:
        cumulative_trade_score = sum(assess["assessment"]["credit_score"] * assess["invoice_amount"] for assess in individual_assessment) / total_invoice_amount
    else:
        cumulative_trade_score = None

    cumulative_assessment = {
        "cumulative_trade_score": round(cumulative_trade_score, 2) if cumulative_trade_score is not None else None,
        "request_id": str(uuid.uuid4()),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_amount_requested": sum(item.get("invoice_amount", 0) for item in individual_assessment),
        "total_invoice_amount": total_invoice_amount
    }

    response = {
        "cumulative_assessment": cumulative_assessment,
        "individual_assessment": individual_assessment
    }
    print("transaction recommendation-2",response)
    # Save the response to S3
    # Save the response to S3
    try:
        s3_key = f"{S3_FOLDER}/{cumulative_assessment['request_id']}.json"
        s3_client.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=json.dumps(response),
            ContentType='application/json'
        )
        print(f"Response saved to S3 at {S3_BUCKET_NAME}/{s3_key}")
    except Exception as e:
        print(f"Failed to save response to S3: {str(e)}")

            
    return jsonify(response), 200

if __name__ == '__main__':
    app.run(debug=True, host="0.0.0.0", port=8113)
