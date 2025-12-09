# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""FastAPI webhook service for receiving payment notifications.

This service provides a simple webhook endpoint that can be called by external
systems (like TrueLayer) to notify about payment status changes.

When a webhook is received with a payment_id, it calls the merchant agent's
dpc_finish tool to finalize the payment.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
import uvicorn

# Add the src directory to Python path so we can import common modules
src_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(src_path))

from common.a2a_message_builder import A2aMessageBuilder
from common.payment_remote_a2a_client import PaymentRemoteA2aClient
from common.a2a_extension_utils import EXTENSION_URI

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(
    title="Payment Webhook Service",
    description="Webhook endpoint for receiving payment notifications",
    version="1.0.0"
)

WEBHOOK_SERVICE_PORT = 8004
MERCHANT_AGENT_URL = "http://localhost:8001/a2a/merchant_agent"


async def call_merchant_agent_dpc_finish(payment_id: str) -> dict:
    """Call merchant agent's dpc_finish tool with the payment_id.

    Args:
        payment_id: The payment ID to send to dpc_finish

    Returns:
        Dictionary with response from merchant agent
    """
    logger.info("Calling merchant agent dpc_finish for payment_id: %s", payment_id)

    try:
        # Create A2A client for merchant agent
        merchant_agent_client = PaymentRemoteA2aClient(
            name="merchant_agent",
            base_url=MERCHANT_AGENT_URL,
            required_extensions={EXTENSION_URI},
        )

        # Build DPC response (mock structure for now)
        # In production, this would be the actual DPC credential from TrueLayer
        dpc_response = {
            "payment_id": payment_id,
            "status": "executed",
            "vp_token": f"mock_jwt_token_for_{payment_id}",
            "presentation_submission": {
                "id": payment_id,
                "definition_id": "payment_credential_request",
                "descriptor_map": []
            }
        }

        # Build A2A message to call dpc_finish
        message = (
            A2aMessageBuilder()
            .set_context_id(f"{payment_id}")
            .add_text("Validate the Digital Payment Credentials (DPC) response")
            .add_data("dpc_response", dpc_response)
            .add_data("shopping_agent_id", "trusted_shopping_agent")
            .build()
        )

        logger.info("Sending dpc_finish message to merchant agent...")
        task = await merchant_agent_client.send_a2a_message(message)

        logger.info("Received response from merchant agent:")
        logger.info("  Task ID: %s", task.id)
        logger.info("  Status: %s", task.status.state)
        logger.info("  Message: %s", task.status.message if task.status.message else "None")

        # Extract payment status from artifacts if available
        payment_status = None
        transaction_id = None
        if task.artifacts:
            for artifact in task.artifacts:
                if hasattr(artifact, 'root') and hasattr(artifact.root, 'data'):
                    data = artifact.root.data
                    if isinstance(data, dict):
                        payment_status = data.get("payment_status")
                        transaction_id = data.get("transaction_id")
                        logger.info("  Payment Status: %s", payment_status)
                        logger.info("  Transaction ID: %s", transaction_id)

        return {
            "success": True,
            "task_id": task.id,
            "task_status": task.status.state,
            "payment_status": payment_status,
            "transaction_id": transaction_id,
        }

    except Exception as e:
        logger.error("Failed to call merchant agent dpc_finish: %s", e, exc_info=True)
        return {
            "success": False,
            "error": str(e),
        }


@app.get("/webhook")
async def webhook_handler(payment_id: str = Query(..., description="Payment ID to log")):
    """Webhook endpoint that receives payment_id as query parameter.

    This endpoint is called by external systems (e.g., TrueLayer) to notify
    about payment events. When called, it:
    1. Logs the payment_id
    2. Calls merchant agent's dpc_finish to finalize the payment

    Args:
        payment_id: The payment ID from the query parameter

    Returns:
        JSON response with status and dpc_finish result

    Example:
        GET /webhook?payment_id=abc123
    """
    timestamp = datetime.utcnow().isoformat()

    logger.info("=" * 60)
    logger.info("Webhook called!")
    logger.info("Timestamp: %s", timestamp)
    logger.info("Payment ID: %s", payment_id)
    logger.info("=" * 60)

    # Call merchant agent's dpc_finish
    dpc_result = await call_merchant_agent_dpc_finish(payment_id)

    logger.info("=" * 60)
    logger.info("dpc_finish call completed")
    logger.info("Success: %s", dpc_result.get("success"))
    if dpc_result.get("success"):
        logger.info("Payment Status: %s", dpc_result.get("payment_status"))
        logger.info("Transaction ID: %s", dpc_result.get("transaction_id"))
    else:
        logger.info("Error: %s", dpc_result.get("error"))
    logger.info("=" * 60)

    return JSONResponse(
        status_code=200 if dpc_result.get("success") else 500,
        content={
            "status": "success" if dpc_result.get("success") else "error",
            "message": "Webhook received and dpc_finish called",
            "payment_id": payment_id,
            "timestamp": timestamp,
            "dpc_finish_result": dpc_result,
        }
    )


@app.get("/health")
async def health_check():
    """Health check endpoint to verify service is running.

    Returns:
        JSON response with service status
    """
    return JSONResponse(
        status_code=200,
        content={
            "status": "healthy",
            "service": "webhook_service",
            "port": WEBHOOK_SERVICE_PORT,
        }
    )


@app.get("/")
async def root():
    """Root endpoint with service information.

    Returns:
        JSON response with service details and usage instructions
    """
    return JSONResponse(
        content={
            "service": "Payment Webhook Service",
            "version": "1.0.0",
            "endpoints": {
                "/webhook": {
                    "method": "GET",
                    "description": "Webhook endpoint for payment notifications",
                    "parameters": {
                        "payment_id": "Payment ID (required query parameter)"
                    },
                    "example": f"http://localhost:{WEBHOOK_SERVICE_PORT}/webhook?payment_id=abc123"
                },
                "/health": {
                    "method": "GET",
                    "description": "Health check endpoint"
                }
            }
        }
    )


def main():
    """Start the webhook service."""
    logger.info("")
    logger.info("=" * 60)
    logger.info("Starting Payment Webhook Service")
    logger.info("=" * 60)
    logger.info("Port: %d", WEBHOOK_SERVICE_PORT)
    logger.info("Webhook URL: http://localhost:%d/webhook?payment_id=<id>", WEBHOOK_SERVICE_PORT)
    logger.info("Health check: http://localhost:%d/health", WEBHOOK_SERVICE_PORT)
    logger.info("=" * 60)
    logger.info("")

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=WEBHOOK_SERVICE_PORT,
        log_level="info",
    )


if __name__ == "__main__":
    main()
