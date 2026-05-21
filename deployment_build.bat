@echo off
REM Build Docker image for file-upload
docker build -t tre-v3-dev . 
REM Tag Docker image for Google Cloud registry
docker tag  tre-v3-dev YOUR_GCP_REGION-docker.pkg.dev/YOUR_GCP_PROJECT_ID/YOUR_REPO/tre-v3-dev
REM Push Docker image to Google Cloud registry
docker push YOUR_GCP_REGION-docker.pkg.dev/YOUR_GCP_PROJECT_ID/YOUR_REPO/tre-v3-dev
echo 
pause
