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

"""Tools for the merchant payment processor agent.

Each agent uses individual tools to handle distinct tasks throughout the
shopping and purchasing process.
"""

from datetime import datetime
from datetime import timezone
import logging
import os
from typing import Any
import uuid

import httpx
import json
from truelayer_signing import sign_with_pem, HttpMethod

from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import DataPart
from a2a.types import Part
from a2a.types import Task
from a2a.types import TaskState
from a2a.types import TextPart
from ap2.types.mandate import PAYMENT_MANDATE_DATA_KEY
from ap2.types.mandate import PaymentMandate
from ap2.types.payment_receipt import PAYMENT_RECEIPT_DATA_KEY
from ap2.types.payment_receipt import PaymentReceipt
from ap2.types.payment_receipt import Success
from common import artifact_utils
from common import message_utils
from common.a2a_extension_utils import EXTENSION_URI
from common.a2a_message_builder import A2aMessageBuilder
from common.payment_remote_a2a_client import PaymentRemoteA2aClient

# Get credentials from environment
TL_DOMAIN = os.getenv("TL_DOMAIN")
TL_SIGNING_KEY_ID = os.getenv("TL_SIGNING_KEY_ID")
TL_SIGNING_PRIVATE_KEY = os.getenv("TL_SIGNING_PRIVATE_KEY")
TL_CLIENT_ID = os.getenv("TL_CLIENT_ID")
TL_CLIENT_SECRET = os.getenv("TL_CLIENT_SECRET")
TL_MERCHANT_ACCOUNT_ID = os.getenv("TL_MERCHANT_ACCOUNT_ID")
TL_BENEFICIARY_NAME = os.getenv("TL_BENEFICIARY_NAME", "Merchant Name")
TL_RETURN_URI = os.getenv("TL_RETURN_URI", "https://console.t7r.dev/redirect-page")

async def initiate_payment(
    data_parts: list[dict[str, Any]],
    updater: TaskUpdater,
    current_task: Task | None,
    debug_mode: bool = False,
) -> None:
  """Handles the initiation of a payment."""
  payment_mandate = message_utils.find_data_part(
      PAYMENT_MANDATE_DATA_KEY, data_parts
  )
  if not payment_mandate:
    error_message = _create_text_parts("Missing payment_mandate.")
    await updater.failed(message=updater.new_agent_message(parts=error_message))
    return

  challenge_response = (
      message_utils.find_data_part("challenge_response", data_parts) or ""
  )
  await _handle_payment_mandate(
      PaymentMandate.model_validate(payment_mandate),
      challenge_response,
      updater,
      current_task,
      debug_mode,
  )


async def _handle_payment_mandate(
    payment_mandate: PaymentMandate,
    challenge_response: str,
    updater: TaskUpdater,
    current_task: Task | None,
    debug_mode: bool = False,
) -> None:
  """Handles a payment mandate.

  If no task is present, it initiates a transaction challenge. If a task
  requires input, it verifies the challenge response and completes the payment.

  Args:
    payment_mandate: The payment mandate containing payment details.
    challenge_response: The response to a transaction challenge, if any.
    updater: The task updater for managing task state.
    current_task: The current task, or None if it's a new payment.
    debug_mode: Whether the agent is in debug mode.
  """
  # Extract payment method type from the mandate
  payment_method_type = (
      payment_mandate.payment_mandate_contents.payment_response.method_name
  )

  # TrueLayer VRP mandate payments skip the challenge and process immediately
  if payment_method_type == "TRUELAYER_VRP_MANDATE":
    await _handle_vrp_mandate_payment(payment_mandate, updater, debug_mode)
    return

  # TrueLayer SIP payments require redirect authorization
  if payment_method_type == "TRUELAYER_SIP":
    # Initiate payment and return redirect URI
    await _initiate_sip_payment(payment_mandate, updater, debug_mode)
    return

  # All other payment methods continue with existing challenge flow
  if current_task is None:
    await _raise_challenge(updater)
    return

  if current_task.status.state == TaskState.input_required:
    await _check_challenge_response_and_complete_payment(
        payment_mandate,
        challenge_response,
        updater,
        debug_mode,
    )
    return


async def _shorten_url(long_url: str) -> str:
  """Shortens a URL using the is.gd URL shortening service.

  Args:
    long_url: The URL to shorten.

  Returns:
    The shortened URL, or the original URL if shortening fails.
  """
  try:
    async with httpx.AsyncClient(verify=False) as client:
      response = await client.get(
          "https://is.gd/create.php",
          params={"format": "simple", "url": long_url},
          timeout=5.0,
      )
      if response.status_code == 200:
        shortened = response.text.strip()
        logging.info("Shortened URL from %s to %s", long_url, shortened)
        return shortened
      else:
        logging.warning("URL shortening failed with status %s, using original URL", response.status_code)
        return long_url
  except Exception as e:
    logging.warning("URL shortening failed: %s, using original URL", e)
    return long_url


async def _initiate_sip_payment(
    payment_mandate: PaymentMandate,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> None:
  """Initiates a TrueLayer SIP payment and returns the redirect URI.

  Args:
    payment_mandate: The payment mandate.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.
  """
  logging.info("Initiating SIP payment for mandate id %s...",
               payment_mandate.payment_mandate_contents.payment_mandate_id)

  payment_mandate_id = (
      payment_mandate.payment_mandate_contents.payment_mandate_id
  )
  credentials_provider = _get_credentials_provider_client(payment_mandate)
  payment_credential = await _request_payment_credential(
      payment_mandate,
      credentials_provider,
      updater,
      debug_mode,
  )

  # Extract user info from payment mandate
  payment_response = payment_mandate.payment_mandate_contents.payment_response
  shipping_address = payment_response.shipping_address

  # Extract amount and currency from payment mandate
  payment_total = payment_mandate.payment_mandate_contents.payment_details_total
  amount = payment_total.amount.value
  currency = payment_total.amount.currency

  # Generate user ID
  user_id = str(uuid.uuid4())
  user_name = shipping_address.recipient if shipping_address else "Unknown"
  user_email = payment_response.payer_email or "unknown@example.com"
  user_phone = shipping_address.phone_number if shipping_address else "+00000000000"

  logging.info(
      "Calling TrueLayer SIP API for payment %s...",
      payment_mandate_id,
  )

  try:
    # Call TrueLayer SIP Payments API
    truelayer_response = await _call_truelayer_payments_api_sip(
        amount=amount,
        currency=currency,
        user_id=user_id,
        user_name=user_name,
        user_email=user_email,
        user_phone=user_phone,
        updater=updater
    )
    logging.info("TrueLayer SIP payment response: %s", truelayer_response)

    # Extract redirect URI from hosted_page
    redirect_uri = truelayer_response.get("hosted_page", {}).get("uri")
    truelayer_payment_id = truelayer_response.get("id")

    # Create a new message with payment_id info
    data_parts = [
        Part(
            root=DataPart(data={"payment_id": truelayer_payment_id})
        )
    ]
    await updater.add_artifact(data_parts)

    if redirect_uri:
      logging.info("TrueLayer SIP redirect URI: %s", redirect_uri)
      logging.info("TrueLayer SIP payment ID: %s", truelayer_payment_id)

      # Shorten the redirect URI for a better user experience
      shortened_uri = await _shorten_url(redirect_uri)
      logging.info(f"Shortened URL: {shortened_uri}");

      # Store payment ID in state for later use when completing payment
      redirect_data = {
          "type": "redirect",
          "redirect_uri": shortened_uri,
          "payment_id": truelayer_payment_id,
          "display_text": (
              f"Please complete your payment authorization by visiting this link: {shortened_uri}"
          ),
      }
      text_part = TextPart(
          text="Please authorize your payment to complete the transaction."
      )
      data_part = DataPart(data={"sip_redirect": redirect_data})
      message = updater.new_agent_message(
          parts=[Part(root=text_part), Part(root=data_part)]
      )
      await updater.requires_input(message=message)
    else:
      raise ValueError("No redirect URI found in SIP payment response")

  except Exception as e:
    logging.error("TrueLayer SIP API call failed: %s", e)
    error_message = _create_text_parts(f"SIP payment initiation failed: {e}")
    await updater.failed(message=updater.new_agent_message(parts=error_message))


async def _initiate_mandate_creation(
    payment_mandate: PaymentMandate,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> None:
  """Initiates a TrueLayer VRP mandate creation and returns the authorization link.

  Args:
    payment_mandate: The payment mandate.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.
  """
  logging.info("Initiating mandate creation for mandate id %s...",
               payment_mandate.payment_mandate_contents.payment_mandate_id)

  payment_mandate_id = (
      payment_mandate.payment_mandate_contents.payment_mandate_id
  )
  credentials_provider = _get_credentials_provider_client(payment_mandate)

  # Extract user info from payment mandate
  payment_response = payment_mandate.payment_mandate_contents.payment_response
  shipping_address = payment_response.shipping_address

  # Generate user ID
  user_id = str(uuid.uuid4())
  user_name = shipping_address.recipient if shipping_address else "Unknown"
  user_email = payment_response.payer_email or "unknown@example.com"

  logging.info(
      "Calling TrueLayer Mandates API for mandate creation...",
  )

  try:
    # Call TrueLayer Mandates API
    truelayer_response = await _call_truelayer_mandates_api(
        user_id=user_id,
        user_name=user_name,
        user_email=user_email,
    )
    logging.info("TrueLayer Mandate creation response: %s", truelayer_response)

    # Extract mandate ID and resource token from response
    mandate_id = truelayer_response.get("id")
    resource_token = truelayer_response.get("resource_token")

    # Create a new message with mandate_id info
    data_parts = [
        Part(
            root=DataPart(data={"mandate_id": mandate_id})
        )
    ]
    await updater.add_artifact(data_parts)

    if mandate_id and resource_token:
      # Build the authorization link
      authorization_link = f"https://payment.{TL_DOMAIN}/mandates#mandate_id={mandate_id}&resource_token={resource_token}&return_uri={TL_RETURN_URI}"

      logging.info("TrueLayer Mandate authorization link: %s", authorization_link)
      logging.info("TrueLayer Mandate ID: %s", mandate_id)

      # Shorten the authorization link for a better user experience
      shortened_link = await _shorten_url(authorization_link)
      logging.info(f"Shortened URL: {shortened_link}")

      # Store the mandate ID in credentials provider for future use
      await _send_vrp_mandate_id_to_credentials_provider(
          user_email=user_email,
          vrp_mandate_id=mandate_id,
          credentials_provider=credentials_provider,
          updater=updater,
          debug_mode=debug_mode,
      )

      # Store mandate ID in state for later use
      redirect_data = {
          "type": "redirect",
          "redirect_uri": shortened_link,
          "mandate_id": mandate_id,
          "display_text": (
              f"Please authorize your VRP mandate by visiting this link: {shortened_link}"
          ),
      }
      text_part = TextPart(
          text="Please authorize the VRP mandate to enable future payments."
      )
      data_part = DataPart(data={"mandate_redirect": redirect_data})
      message = updater.new_agent_message(
          parts=[Part(root=text_part), Part(root=data_part)]
      )
      await updater.requires_input(message=message)
    else:
      raise ValueError("No mandate ID or resource token found in mandate creation response")

  except Exception as e:
    logging.error("TrueLayer Mandate API call failed: %s", e)
    error_message = _create_text_parts(f"Mandate creation failed: {e}")
    await updater.failed(message=updater.new_agent_message(parts=error_message))


async def _raise_challenge(
    updater: TaskUpdater,
) -> None:
  """Raises a transaction challenge.

  This challenge would normally be raised by the issuer, but we don't
  have an issuer in the demo, so we raise the challenge here. For concreteness,
  we are using an OTP challenge in this sample.

  Args:
    updater: The task updater.
  """
  challenge_data = {
      "type": "otp",
      "display_text": (
          "The payment method issuer sent a verification code to the phone "
          "number on file, please enter it below. It will be shared with the "
          "issuer so they can authorize the transaction."
          "(Demo only hint: the code is 123)"
      ),
  }
  text_part = TextPart(
      text="Please provide the challenge response to complete the payment."
  )
  data_part = DataPart(data={"challenge": challenge_data})
  message = updater.new_agent_message(
      parts=[Part(root=text_part), Part(root=data_part)]
  )
  await updater.requires_input(message=message)


async def _check_challenge_response_and_complete_payment(
    payment_mandate: PaymentMandate,
    challenge_response: str,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> None:
  """Checks the challenge response and completes the payment process.

  Checking the challenge response would be done by the issuer, but we don't
  have an issuer in the demo, so we do it here.

  Args:
    payment_mandate: The payment mandate.
    challenge_response: The challenge response.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.
  """
  if _challenge_response_is_valid(challenge_response=challenge_response):
    await _handle_vrp_mandate_payment(payment_mandate, updater, debug_mode)
    return

  message = updater.new_agent_message(
      _create_text_parts("Challenge response incorrect.")
  )
  await updater.requires_input(message=message)


async def _handle_vrp_mandate_payment(
    payment_mandate: PaymentMandate,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> None:
  """Completes the payment process.

  Args:
    payment_mandate: The payment mandate.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.
  """
  logging.info("Completing payment for mandate id %s...",
               payment_mandate.payment_mandate_contents.payment_mandate_id)

  payment_mandate_id = (
      payment_mandate.payment_mandate_contents.payment_mandate_id
  )
  credentials_provider = _get_credentials_provider_client(payment_mandate)
  payment_credential = await _request_payment_credential(
      payment_mandate,
      credentials_provider,
      updater,
      debug_mode,
  )

  # Check if this is a TRUELAYER_VRP_MANDATE payment
  payment_method_type = (
      payment_mandate.payment_mandate_contents.payment_response.method_name
  )

  truelayer_payment_id = None

  if payment_method_type == "TRUELAYER_VRP_MANDATE":
    # Extract VRP mandate ID from credentials
    logging.info("Payment credential for VRP mandate: %s", payment_credential)
    vrp_mandate_id = payment_credential.get("vrp_mandate_id")

    if not vrp_mandate_id:
      logging.info("VRP mandate ID not found in credentials, initiating mandate creation...")
      # Create the VRP mandate and return authorization link
      await _initiate_mandate_creation(payment_mandate, updater, debug_mode)
      return

    logging.info("Using VRP mandate ID: %s", vrp_mandate_id)

    # Extract amount and currency from payment mandate
    payment_total = payment_mandate.payment_mandate_contents.payment_details_total
    amount = payment_total.amount.value
    currency = payment_total.amount.currency

    logging.info(
        "Calling TrueLayer API for payment %s with VRP mandate %s...",
        payment_mandate_id,
        vrp_mandate_id,
    )

    try:
      # Call TrueLayer Payments API
      truelayer_response = await _call_truelayer_payments_api_vrp(
          vrp_mandate_id=vrp_mandate_id,
          amount=amount,
          currency=currency,
      )
      logging.info("TrueLayer payment response: %s", truelayer_response)
      # Extract TrueLayer payment ID from response
      truelayer_payment_id = truelayer_response.get("id")
      if truelayer_payment_id:
        logging.info("TrueLayer payment ID: %s", truelayer_payment_id)
    except Exception as e:
      logging.error("TrueLayer API call failed: %s", e)
      # Continue with creating receipt for demo purposes
  else:
    # Original flow for CARD payments
    logging.info(
        "Calling issuer to complete payment for %s with payment credential %s...",
        payment_mandate_id,
        payment_credential,
    )

  # Create payment receipt, using TrueLayer payment ID if available
  payment_receipt = _create_payment_receipt(
      payment_mandate,
      payment_id=truelayer_payment_id
  )
  if truelayer_payment_id:
    logging.info("Creating receipt with TrueLayer payment ID: %s", truelayer_payment_id)
  await _send_payment_receipt_to_credentials_provider(
      payment_receipt,
      credentials_provider,
      updater,
      debug_mode,
  )
  await updater.add_artifact([
      Part(
          root=DataPart(
              data={PAYMENT_RECEIPT_DATA_KEY: payment_receipt.model_dump()}
          )
      )
  ])
  success_message = updater.new_agent_message(
      parts=_create_text_parts("{'status': 'success'}")
  )
  await updater.complete(message=success_message)


def _challenge_response_is_valid(challenge_response: str) -> bool:
  """Validates the challenge response."""

  return challenge_response == "123"


async def _get_truelayer_access_token() -> str:
  """Obtains an access token from TrueLayer OAuth endpoint.

  Returns:
    Access token string
  """
  token_url = f"https://auth.{TL_DOMAIN}/connect/token"

  logging.info("Requesting TrueLayer access token from %s...", token_url)

  # Prepare form data
  form_data = {
      "client_id": TL_CLIENT_ID,
      "client_secret": TL_CLIENT_SECRET,
      "grant_type": "client_credentials",
      "scope": "payments recurring_payments:sweeping recurring_payments:commercial",
  }

  async with httpx.AsyncClient() as client:
    response = await client.post(
        token_url,
        data=form_data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    response.raise_for_status()
    token_data = response.json()
    access_token = token_data["access_token"]
    logging.info("Successfully obtained TrueLayer access token")
    return access_token


async def _call_truelayer_payments_api_vrp(
    vrp_mandate_id: str,
    amount: float,
    currency: str
) -> dict:
  """Calls TrueLayer Payments API to execute payment via VRP mandate.

  Args:
    vrp_mandate_id: The VRP mandate ID from credentials
    amount: Payment amount in major currency units (e.g., 10.50)
    currency: Three-letter ISO currency code (e.g., "GBP")

  Returns:
    API response as dictionary
  """

  # Dynamically obtain access token
  access_token = await _get_truelayer_access_token()

  # Convert amount to minor units (e.g., dollars to cents)
  amount_in_minor = int(amount * 100)

  # Generate idempotency key
  idempotency_key = str(uuid.uuid4())

  # Prepare payload with consistent JSON formatting for signature
  # Note: Hardcoding GBP for TrueLayer API regardless of the payment mandate currency.
  # In a real implementation, currency conversion would be handled, but for this demo
  # we don't care about the currency mismatch.
  payload = {
      "payment_method": {
          "type": "mandate",
          "mandate_id": vrp_mandate_id,
      },
      "amount_in_minor": amount_in_minor,
      "currency": "GBP",
  }
  body = json.dumps(payload, separators=(",", ":"))

  # Generate TrueLayer signature
  tl_signature = (
      sign_with_pem(TL_SIGNING_KEY_ID, TL_SIGNING_PRIVATE_KEY)
      .set_method(HttpMethod.POST)
      .set_path("/payments")
      .add_header("Idempotency-Key", idempotency_key)
      .set_body(body)
      .sign()
  )

  # Prepare request headers
  url = f"https://api.{TL_DOMAIN}/payments"
  headers = {
      "Authorization": f"Bearer {access_token}",
      "Content-Type": "application/json",
      "Idempotency-Key": idempotency_key,
      "Tl-Signature": tl_signature,
  }

  async with httpx.AsyncClient() as client:
    response = await client.post(url, headers=headers, data=body)
    response.raise_for_status()
    return response.json()


async def _call_truelayer_payments_api_sip(
    amount: float,
    currency: str,
    user_id: str,
    user_name: str,
    user_email: str,
    user_phone: str,
    updater: TaskUpdater
) -> dict:
  """Calls TrueLayer Payments API to create a Single Immediate Payment (SIP).

  Args:
    amount: Payment amount in major currency units (e.g., 10.50)
    currency: Three-letter ISO currency code (e.g., "GBP")
    user_id: User identifier
    user_name: User's full name
    user_email: User's email address
    user_phone: User's phone number

  Returns:
    API response as dictionary
  """

  # Dynamically obtain access token
  access_token = await _get_truelayer_access_token()

  # Convert amount to minor units (e.g., dollars to cents)
  amount_in_minor = int(amount * 100)

  # Generate idempotency key
  idempotency_key = str(uuid.uuid4())

  # Prepare payload for SIP
  # Note: Hardcoding GBP for TrueLayer API regardless of the payment mandate currency.
  payload = {
      "amount_in_minor": amount_in_minor,
      "currency": "GBP",
      "payment_method": {
          "provider_selection": {
              "type": "user_selected"
          },
          "type": "bank_transfer",
          "beneficiary": {
              "type": "merchant_account",
              "account_holder_name": TL_BENEFICIARY_NAME,
              "merchant_account_id": TL_MERCHANT_ACCOUNT_ID,
          }
      },
      "hosted_page": {
        "return_uri": TL_RETURN_URI
      },
      "user": {
          "id": user_id,
          "name": user_name,
          "email": user_email,
          "phone": user_phone,
      }
  }
  body = json.dumps(payload, separators=(",", ":"))

  # Generate TrueLayer signature
  try:
    tl_signature = (
        sign_with_pem(TL_SIGNING_KEY_ID, TL_SIGNING_PRIVATE_KEY)
        .set_method(HttpMethod.POST)
        .set_path("/payments")
        .add_header("Idempotency-Key", idempotency_key)
        .set_body(body)
        .sign()
    )
  except Exception as e:
    logging.error("Error generating TrueLayer signature: %s", e)

  # Prepare request headers
  url = f"https://api.{TL_DOMAIN}/payments"
  headers = {
      "Authorization": f"Bearer {access_token}",
      "Content-Type": "application/json",
      "Idempotency-Key": idempotency_key,
      "Tl-Signature": tl_signature,
  }

  async with httpx.AsyncClient() as client:
    response = await client.post(url, headers=headers, data=body)
    response.raise_for_status()
    return response.json()


async def _call_truelayer_mandates_api(
    user_id: str,
    user_name: str,
    user_email: str,
) -> dict:
  """Calls TrueLayer Mandates API to create a commercial mandate.

  Args:
    user_id: User identifier
    user_name: User's full name
    user_email: User's email address

  Returns:
    API response as dictionary containing mandate details and authorization URI
  """

  # Dynamically obtain access token
  access_token = await _get_truelayer_access_token()

  # Generate idempotency key
  idempotency_key = str(uuid.uuid4())

  # Get current time and set validity period
  # Format as ISO-8601 with milliseconds: YYYY-MM-DDTHH:mm:ss.sssZ
  from datetime import timedelta
  now = datetime.now(timezone.utc)
  valid_from = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
  # Set valid_to to 1 year from now
  valid_to = (now + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

  # Prepare payload for mandate creation
  # Note: Hardcoding constraints and beneficiary details for demo purposes
  payload = {
      "mandate": {
          "type": "commercial",
          "provider_filter": {
              "countries": ["GB"],
              "release_channel": "private_beta",
          },
          "provider_selection": {
              "type": "user_selected"
          },
          "beneficiary": {
              "type": "external_account",
              "account_holder_name": "Beneficiary Name",
              "account_identifier": {
                  "type": "sort_code_account_number",
                  "sort_code": "100000",
                  "account_number": "31510604",
              }
          }
      },
      "currency": "GBP",
      "user": {
          "id": user_id,
          "name": user_name,
          "email": user_email,
          "phone": "+44123456789",
      },
      "constraints": {
          "valid_from": valid_from,
          "valid_to": valid_to,
          "maximum_individual_amount": 50,
          "periodic_limits": {
              "day": {
                  "maximum_amount": 100,
                  "period_alignment": "calendar"
              },
              "month": {
                  "maximum_amount": 1000,
                  "period_alignment": "calendar"
              }
          }
      }
  }
  body = json.dumps(payload, separators=(",", ":"))

  # Generate TrueLayer signature
  tl_signature = (
      sign_with_pem(TL_SIGNING_KEY_ID, TL_SIGNING_PRIVATE_KEY)
      .set_method(HttpMethod.POST)
      .set_path("/mandates")
      .add_header("Idempotency-Key", idempotency_key)
      .set_body(body)
      .sign()
  )

  # Prepare request headers
  url = f"https://api.{TL_DOMAIN}/mandates"
  headers = {
      "Authorization": f"Bearer {access_token}",
      "Content-Type": "application/json",
      "Idempotency-Key": idempotency_key,
      "Tl-Signature": tl_signature,
  }

  async with httpx.AsyncClient() as client:
    response = await client.post(url, headers=headers, data=body)
    response.raise_for_status()
    return response.json()

async def get_payment_status(data_parts: list[dict[str, Any]],
    updater: TaskUpdater,
    current_task: Task | None,
    debug_mode: bool = False) -> None:
    """Handles polling and checking of a payment status."""

    payment_id = (
        message_utils.find_data_part("payment_id", data_parts)
    )
    if not payment_id:
        raise ValueError("Missing payment_id.")

    # Get access token
    access_token = await _get_truelayer_access_token()
    idempotency_key = str(uuid.uuid4())

    url = f"https://api.{TL_DOMAIN}/payments/{payment_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotency_key,
    }

    async with httpx.AsyncClient() as client:
       response = await client.get(url, headers=headers)
       response.raise_for_status()

    await updater.add_artifact([
        Part(
            root=DataPart(
                data={
                    "payment_id": payment_id,
                    "payment_status": response.json().get("status"),
                }
            )
        )
    ])
    await updater.complete()
    return

async def get_mandate_status(
    data_parts: list[dict[str, Any]],
    updater: TaskUpdater,
    current_task: Task | None,
    debug_mode: bool = False
) -> None:
    """Handles polling and checking of a mandate status.

    Args:
        data_parts: DataPart contents containing mandate_id
        updater: The task updater
        current_task: The current task
        debug_mode: Whether the agent is in debug mode
    """
    mandate_id = message_utils.find_data_part("mandate_id", data_parts)
    if not mandate_id:
        raise ValueError("Missing mandate_id.")

    logging.info("Getting mandate status for mandate_id: %s", mandate_id)

    # Get access token
    access_token = await _get_truelayer_access_token()

    # Generate TrueLayer signature for GET request
    tl_signature = (
        sign_with_pem(TL_SIGNING_KEY_ID, TL_SIGNING_PRIVATE_KEY)
        .set_method(HttpMethod.GET)
        .set_path(f"/mandates/{mandate_id}/")
        .sign()
    )

    url = f"https://api.{TL_DOMAIN}/mandates/{mandate_id}/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Tl-Signature": tl_signature,
    }

    async with httpx.AsyncClient() as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()

    mandate_response = response.json()
    logging.info("Mandate status response: %s", mandate_response)

    await updater.add_artifact([
        Part(
            root=DataPart(
                data={
                    "mandate_id": mandate_id,
                    "mandate_status": mandate_response.get("status"),
                    "mandate_details": mandate_response,
                }
            )
        )
    ])
    await updater.complete()
    return

async def _request_payment_credential(
    payment_mandate: PaymentMandate,
    credentials_provider: PaymentRemoteA2aClient,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> str:
  """Sends a request to the Credentials Provider for payment credentials.

  Args:
    payment_mandate: The PaymentMandate containing payment details.
    credentials_provider: The credentials provider client.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.

  Returns:
    payment_credential: The payment credential details.
  """
  message_builder = (
      A2aMessageBuilder()
      .set_context_id(updater.context_id)
      .add_text("Give me the payment method credentials for the given token.")
      .add_data(PAYMENT_MANDATE_DATA_KEY, payment_mandate.model_dump())
      .add_data("debug_mode", debug_mode)
  )
  task = await credentials_provider.send_a2a_message(message_builder.build())

  if not task.artifacts:
    raise ValueError("Failed to find the payment method data.")
  payment_credential = artifact_utils.get_first_data_part(task.artifacts)

  return payment_credential


def _create_payment_receipt(
    payment_mandate: PaymentMandate,
    payment_id: str | None = None
) -> PaymentReceipt:
  """Creates a payment receipt.

  Args:
    payment_mandate: The PaymentMandate containing payment details.
    payment_id: Optional payment ID. If not provided, generates a random UUID.

  Returns:
    The PaymentReceipt containing payment receipt details.
  """
  if payment_id is None:
    payment_id = uuid.uuid4().hex

  return PaymentReceipt(
      payment_mandate_id=payment_mandate.payment_mandate_contents.payment_mandate_id,
      timestamp=datetime.now(timezone.utc).isoformat(),
      payment_id=payment_id,
      amount=payment_mandate.payment_mandate_contents.payment_details_total.amount,
      payment_status=Success(
          merchant_confirmation_id=payment_id,
          psp_confirmation_id=payment_id
      ),
      payment_method_details={
          "method_name": (
              payment_mandate.payment_mandate_contents.payment_response.method_name
          )
      },
  )


def _get_credentials_provider_client(
    payment_mandate: PaymentMandate,
) -> PaymentRemoteA2aClient:
  """Gets the credentials provider client.

  Args:
    payment_mandate: The PaymentMandate containing payment details.

  Returns:
    The credentials provider client.
  """
  token_object = (
      payment_mandate.payment_mandate_contents.payment_response.details.get(
          "token"
      )
  )
  credentials_provider_url = token_object.get("url")
  return PaymentRemoteA2aClient(
      name="credentials_provider",
      base_url=credentials_provider_url,
      required_extensions={EXTENSION_URI},
  )


async def _send_payment_receipt_to_credentials_provider(
    payment_receipt: PaymentReceipt,
    credentials_provider: PaymentRemoteA2aClient,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> None:
  """Sends the payment receipt to the Credentials Provider.

  Args:
    payment_receipt: The PaymentReceipt containing payment receipt details.
    credentials_provider: The credentials provider client.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.
  """

  message_builder = (
      A2aMessageBuilder()
      .set_context_id(updater.context_id)
      .add_text("Here is the payment receipt. No action is required.")
      .add_data(PAYMENT_RECEIPT_DATA_KEY, payment_receipt.model_dump())
      .add_data("debug_mode", debug_mode)
  )
  await credentials_provider.send_a2a_message(message_builder.build())


async def _send_vrp_mandate_id_to_credentials_provider(
    user_email: str,
    vrp_mandate_id: str,
    credentials_provider: PaymentRemoteA2aClient,
    updater: TaskUpdater,
    debug_mode: bool = False,
) -> None:
  """Sends the VRP mandate ID to the Credentials Provider to store as a payment method.

  Args:
    user_email: Email of the user.
    vrp_mandate_id: ID of the TrueLayer VRP mandate.
    credentials_provider: The credentials provider client.
    updater: The task updater.
    debug_mode: Whether the agent is in debug mode.
  """
  # Generate a unique payment method ID
  payment_method_id = "truelayer_vrp"

  # Prepare payment method data matching the credentials provider's expected format
  payment_method_data = {
      "alias": "TrueLayer VRP mandate",
      "brand": "TrueLayer",
      "network": [{"name": "truelayer"}],
      "account_number": vrp_mandate_id[:8],
      "vrp_mandate_id": vrp_mandate_id,
  }

  message_builder = (
      A2aMessageBuilder()
      .set_context_id(updater.context_id)
      .add_text("Store this VRP mandate as a payment method for the user.")
      .add_data("email_address", user_email)
      .add_data("payment_method_id", payment_method_id)
      .add_data("payment_method_type", "TRUELAYER_VRP_MANDATE")
      .add_data("payment_method_data", payment_method_data)
      .add_data("debug_mode", debug_mode)
  )

  logging.info(
      "Sending VRP mandate ID %s to credentials provider for user %s",
      vrp_mandate_id,
      user_email,
  )

  await credentials_provider.send_a2a_message(message_builder.build())


def _create_text_parts(*texts: str) -> list[Part]:
  """Helper to create text parts."""
  return [Part(root=TextPart(text=text)) for text in texts]
