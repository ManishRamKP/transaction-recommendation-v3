# Use an official Python runtime as the base image
FROM python:3.8-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1
ENV FLASK_APP=trxn_reco_gcp.py

# Set work directory
WORKDIR /app
COPY trxn_reco_gcp.py /app
COPY requirements.txt /app
COPY config_gcp.json /app


# Force APT to use IPv4
RUN echo 'Acquire::ForceIPv4 "true";' > /etc/apt/apt.conf.d/99force-ipv4

# Install system dependencies
RUN apt-get update \
    && apt-get -y install gcc \
    && apt-get clean

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip
RUN pip install -r requirements.txt

# Copy project
COPY . /app

# Expose the port the app runs on
EXPOSE 8113

# Run the application
CMD ["python", "trxn_reco_gcp.py"]
