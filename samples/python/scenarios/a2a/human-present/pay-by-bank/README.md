# Agent Payments Protocol Sample: Pay by Bank

This sample demonstrates the A2A ap2-extension for a human present transaction
using TrueLayer's *Pay By Bank* as the payment method.

## Supported scenarios

Currently, we support 3 different scenarios:
1. Single immediate payments
2. VRP commercial mandate for a returning user
3. VRP commercial mandate for a new user

### Single immediate payments

In the single immediate payment scenario, the shopping agent yields a link to our Hosted Payments page where the user can authorise and complete the payment on a selected bank. Once the payment is completed, a receipt is created and shown to the user for confirmation.

### VRP commercial payments

In VRP cases, if the user is a returning one (`bugsbunny@gmail.com` in our example) they are simply requested to confirm the payment, without any further authentication step on the bank.

If the user is a new user (`daffyduck@gmail.com` in our sample), the user is requested to authorise the VRP mandate on the bank first, and only once the authorisation completes, the shopping agent proceeds creating a payment using the mandate just created.

In both cases, similarly to what happens in the single payment scenario, the user is shown a payment receipt confirming the transaction

## Key Actors

This sample consists of:

*  **Shopping Agent:** The main orchestrator that handles user's requests to
    shop and delegates tasks to specialized agents.
*   **Merchant Agent:** An agent that handles product queries from the shopping
    agent.
*   **Merchant Payment Processor Agent:** An agent that takes payments on behalf
    of the merchant.
*   **Credentials Provider Agent:** The credentials provider is the holder of a
    user's payment credentials.

To use be able to TrueLayer payment methods we had to slightly modify the implementation available in the cards sample. More precisely...

### Shopping agent changes

We had to amend the shopping agent prompt so that we could handle resuming a payment flow after a VRP mandate or one-off payment consent were authorised on the user's bank.


### Credentials providers use

The credentials provider is used as vault for payment methods issued by TrueLayer. Rather than storing card details, we imagine the credentials provider agent as vault for payments consents, both for one-off payments and pre-authorized Variable Recurring Payment (VRP) mandates. The credentials provider exposes tools to:
- List eligible bank accounts associated with the user’s TrueLayer consents
- Retrieve stored VRP mandate identifiers and their active status
- Issue short-lived payment initiation tokens for new payments
- Attach payment method details (consent ID or mandate ID) into the PaymentMandate

At the minute, for the purpose of the demo, only VRP-related directives have been implemented.

### Merchant Payment Processor Agent

Here is where we have integrated our payments and mandates APIs.

## Sequence diagram

```mermaid
sequenceDiagram
    participant U as User
    participant SA as Shopping<br/>Agent
    participant CPA as Credential Provider<br/>Agent
    participant MA as Merchant<br/>Agent
    participant MPP as Merchant<br/>Payment Processor<br/>Agent
    participant TL as TrueLayer
    participant B as Bank

    U->>SA: "I would like to buy a pizza"
    Note over SA: Intent Mandate<br/>( contains user search details<br/>e.g: price cap, merchant, ... )
    SA->>U: Show Intent Mandate
    U->>SA: Approve
    SA->>MA: Search for products
    Note over MA: Cart Mandate<br/>( proposed products )
    MA->>SA: Return candidate products
    SA->>U: Show Cart Mandate
    U->>SA: Select product/s
    SA->>MA: Send Cart Mandate
    MA->>MA: Sign Cart Mandate
    MA->>SA: Return signed Cart Mandate
    SA->>CPA: Request shipping options
    CPA->>SA: Provide shipping options
    SA->>U: Provide shipping options
    U->>SA: Select shipping address
    SA->>CPA: Request payment options
    CPA->>SA: Provide payment options
    SA->>U: Provide payment options
    U->>SA: Select pament method
    Note over SA: Payment Mandate<br/>( selected product/s<br/>+ shipping address<br/>+ payment method )
    SA->>U: Show Payment Mandate
    U->>U: Sign Payment Mandate
    U->>SA: Approve
    SA->>MA: Initiate payment
    MA->>MPP: Initiate payment
    MPP->>CPA: Request payment credentials
    CPA->>MPP: Provide payment credentials
    MPP->>TL: Initiate payment
    TL->>B: Initiate payment
    B-->>U: Strong customer authentication
    U-->>B: Confirm
    B-->>TL: Confirm
    TL->>MPP: Complete transaction
    MPP->>MA: Payment Receipt
    MA->>SA: Payment Receipt
    SA->>U: Payment Receipt
    MPP->>CPA: Store Payment Receipt
```

## Executing the Example

### Setup

Ensure you have obtained a Google API key from
[Google AI Studio](https://aistudio.google.com/apikey). You also need to set up
TrueLayer credentials including a mandate ID, client credentials, and signing
keys. Declare the required variables:

```sh
export GOOGLE_API_KEY=your_google_api_key
export TL_DOMAIN=truelayer-sandbox.com
export TL_MANDATE_ID=your_mandate_id
export TL_CLIENT_ID=your_client_id
export TL_CLIENT_SECRET=your_client_secret
export TL_SIGNING_KEY_ID=your_signing_key_id
export TL_SIGNING_PRIVATE_KEY=your_private_key_pem
export TL_MERCHANT_ACCOUNT_ID=your_merchant_account_id
export TL_BENEFICIARY_NAME=your_beneficiary_name
export TL_RETURN_URI=https://console.truelayer-sandbox.com/redirect-page
```

Alternatively, put them into an .env file at the root of your repository.

> [!NOTE]
> Most of these environment variables all represent your TrueLayer client credentials and/or payment routing information. As such, they must be prepared and retrieved with the help of [our standard integration guides and console](https://docs.truelayer.com/docs/quickstart-create-a-console-account).
>
>the `TL_DOMAIN` is used to route requests to a specific TrueLayer environment, Sandbox in this case.
>
>the `TL_MANDATE_ID` environment variable is only used to quickly demo the returning case for a VRP mandate payment, and it should be populated with an existing VRP mandate id previously created on TrueLayer. In a real-world scenario, if the mandate is not found, it will be first authorised and then used as payment method.

### Execution

Tu run our sample, we strongly suggest using the bash utility present in the `pay-by-bank` sample folder (cloned from the cards sample).

```sh
bash samples/python/scenarios/a2a/human-present/pay-by-bank/run.sh
```

Then, open a browser and navigate to the shopping agent UI at http://0.0.0.0:8000. You
may now begin interacting with the Shopping Agent.

Refer to the [cards sample](../cards/README.md) for more details on how to troubleshoot the execution of the agent.
