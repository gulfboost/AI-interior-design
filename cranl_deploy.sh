#!/bin/bash
# Deployment script for CranL
# Usage: ./cranl_deploy.sh

set -e

echo "=== Pre-flight Checks ==="
if [ -f .env ]; then
  echo "Loading .env file..."
  export $(grep -v '^#' .env | xargs)
fi

# Validate essential env vars
if [[ -z "$DATABASE_URL" ]] || [[ "$DATABASE_URL" == *"localhost"* ]]; then
  echo "ERROR: DATABASE_URL is missing or points to localhost. It must be a production Postgres URL."
  exit 1
fi

if [[ -z "$REDIS_URL" ]] || [[ "$REDIS_URL" == *"localhost"* ]]; then
  echo "ERROR: REDIS_URL is missing or points to localhost. It must be a production Redis URL."
  exit 1
fi

if [[ -z "$OPENROUTER_API_KEY" ]]; then
  echo "ERROR: OPENROUTER_API_KEY is missing."
  exit 1
fi

# URLs mapped from CranL dashboard
CRANL_BACKEND_URL="https://ai-styling-backend-uzfwne.cranl.net"
CRANL_FRONTEND_URL="https://ai-styling-frontend-imhc7f.cranl.net"

REPO_ID="23bd2e35-51ff-48ba-9dff-85f0e35b7194" # gulfboost/AI-interior-design

echo "=== Deploying Backend ==="
# cranl apps create --repo $REPO_ID --name ai-styling-backend --build-type nixpacks --branch main --build-path api

BACKEND_ID="ad58e2b2-367a-4234-bf35-96ef1547b460"
FRONTEND_ID="be3f1125-7200-4d2b-ac47-0e0dc1e32ca2"

cranl apps env set $BACKEND_ID GCP_PROJECT_ID=gulfboost-odoo-login
cranl apps env set $BACKEND_ID GCS_BUCKET=ai-home-styling-poc
cranl apps env set $BACKEND_ID OPENROUTER_API_KEY="$OPENROUTER_API_KEY"
cranl apps env set $BACKEND_ID GOOGLE_CLOUD_API_KEY="$GOOGLE_CLOUD_API_KEY"
cranl apps env set $BACKEND_ID API_URL="$CRANL_BACKEND_URL"
cranl apps env set $BACKEND_ID FRONTEND_URL="$CRANL_FRONTEND_URL"
cranl apps env set $BACKEND_ID DATABASE_URL="$DATABASE_URL"
cranl apps env set $BACKEND_ID REDIS_URL="$REDIS_URL"
cranl apps env set $BACKEND_ID APP_TYPE="backend"

echo "=== Deploying Frontend ==="
# cranl apps create --repo $REPO_ID --name ai-styling-frontend --build-type nixpacks --branch main
cranl apps env set $FRONTEND_ID NEXT_PUBLIC_API_URL="$CRANL_BACKEND_URL"
cranl apps env set $FRONTEND_ID APP_TYPE="frontend"

echo "=== Triggering Builds ==="
cranl apps deploy $BACKEND_ID
cranl apps deploy $FRONTEND_ID

echo "Done!"
echo "Backend: $CRANL_BACKEND_URL"
echo "Frontend: $CRANL_FRONTEND_URL"
