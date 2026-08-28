# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: 2026 benelog GmbH & Co. KG
"""The Core Business Vocabulary, as far as a connector needs it.

EPCIS says *what* happened in a fixed grammar; the CBV supplies the words. A
receipt is not "goods in" and not "wareneingang" — it is ``receiving``, and an
observer three companies away reads it without a mapping table. That is the
entire value of the standard, and it is why these are constants rather than
free text in the host adapter.

The names here are the ones an ERP actually reaches for. They are not the whole
vocabulary, and nothing in this package refuses a value that is missing: the
CBV is extensible, a trading partner may agree on a term of their own, and a
library that policed the list would break that agreement rather than the
mistake it was trying to catch. What the constants buy is that the common
twenty are spelled the same way everywhere.

Values are the bare CBV tokens, not the full ``urn:epcglobal:cbv:...`` URIs.
EPCIS 2.0 in JSON-LD expands them through its context, and the repository
stores them expanded either way.
"""

# -- Actions ----------------------------------------------------------------
#
# What the event says about the identifiers' existence, not about the goods:
# ADD is "these came into being here", DELETE "these ceased to be", OBSERVE
# "these were seen, and they existed before and after". Almost every movement
# is an OBSERVE; only creation and destruction are the other two.

ADD = "ADD"
OBSERVE = "OBSERVE"
DELETE = "DELETE"

# -- Business steps ---------------------------------------------------------

COMMISSIONING = "commissioning"
DECOMMISSIONING = "decommissioning"
RECEIVING = "receiving"
SHIPPING = "shipping"
PICKING = "picking"
PACKING = "packing"
UNPACKING = "unpacking"
STORING = "storing"
STOCK_TAKING = "stock_taking"
INSPECTING = "inspecting"
LOADING = "loading"
UNLOADING = "unloading"
RETAIL_SELLING = "retail_selling"
DESTROYING = "destroying"
REPACKAGING = "repackaging"
TRANSPORTING = "transporting"
ENTERING_EXITING = "entering_exiting"

# -- Dispositions -----------------------------------------------------------
#
# The state the goods are left in. A disposition persists until another event
# changes it, which is what makes it worth stating: "in_transit" answers a
# question days after the shipping event, "shipping" only answers it once.

ACTIVE = "active"
IN_PROGRESS = "in_progress"
IN_TRANSIT = "in_transit"
SELLABLE_ACCESSIBLE = "sellable_accessible"
SELLABLE_NOT_ACCESSIBLE = "sellable_not_accessible"
RETAIL_SOLD = "retail_sold"
NON_SELLABLE_OTHER = "non_sellable_other"
DAMAGED = "damaged"
EXPIRED = "expired"
RECALLED = "recalled"
DESTROYED = "destroyed"
INACTIVE = "inactive"

# -- Business transaction types ---------------------------------------------
#
# The paperwork an event belongs to. This is the join between the physical
# record and the commercial one, and it is the reason a warehouse event can
# answer "which order was this?" without the asker having access to the ERP.

PO = "po"
POC = "poc"
INV = "inv"
DESADV = "desadv"
RECADV = "recadv"
PROD_ORDER = "prodorder"
BOL = "bol"

# -- Source and destination types -------------------------------------------

OWNING_PARTY = "owning_party"
POSSESSING_PARTY = "possessing_party"
LOCATION = "location"
