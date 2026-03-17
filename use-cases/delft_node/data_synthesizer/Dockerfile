# Use official, lightweight Python image
FROM python:3.10-slim

# Set the working directory inside the container
WORKDIR /app

# Install git (required to pip install from a GitHub repo)
RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*

# Copy the requirements file into the container
COPY requirements.txt .

# Install dependencies (no-cache-dir keeps the image size small)
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code into the container
COPY common/ ./common/
COPY main.py .

# Configuration files (for orchestrator reference)
COPY blueprint.json .
COPY dockerinfo.json .

# Environment variables for the AI-Effect control interface
ENV SELF_URL=http://localhost:8080
ENV HOST=0.0.0.0
ENV PORT=8080

# Expose the port the API will run on
EXPOSE 8080

# Command to run the service
CMD ["python", "main.py"]