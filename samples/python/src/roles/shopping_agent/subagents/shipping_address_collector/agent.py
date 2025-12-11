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

"""An agent responsible for collecting the user's shipping address.

The shopping agent delegates responsibility for collecting the user's shipping
address to this subagent, after the user has chosen a product.

In this sample, the shopping agent assumes it must collect the shipping address
before finalizing the cart, as it may impact costs such as shipping and tax.

Also in this sample, the shopping agent offers the user the option of using a
digital wallet to provide their shipping address.

This is just one of many possible approaches.
"""

from . import tools
from common.retrying_llm_agent import RetryingLlmAgent
from common.system_utils import DEBUG_MODE_INSTRUCTIONS

shipping_address_collector = RetryingLlmAgent(
    model="gemini-2.5-flash",
    name="shipping_address_collector",
    max_retries=5,
    instruction="""
        You are an agent responsible for obtaining the user's shipping address.

    %s

        When asked to complete a task, follow these instructions:
        1. If you have the user's username, search for their shipping address using the
           'get_shipping_address' tool.
        2. If you can't retrieve the shipping address, collect it from the user
           manually.

        Scenario 1:
        1. If you have the username, search for the user's shipping address using
        the 'get_shipping_address' tool. Format it in a nice way, don't return an explicit json.

        Scenario 2:
        Condition: The user needs to enter their shipping address manually.
        Instructions:
        1. Collect the user's shipping address. Ensure you have collected all
           of the necessary parts of a US address.
        2. Transfer back to the root_agent with the shipping address.
    """ % DEBUG_MODE_INSTRUCTIONS,
    tools=[
        tools.get_shipping_address,
    ],
)
