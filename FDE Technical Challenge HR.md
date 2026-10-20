# FDE Technical Challenge

## Inbound Carrier Sales Automation

*HappyRobot • FDE Technical Challenge*

---

## About Crystal Lane Freight

Crystal Lane Freight is a mid-sized freight brokerage based in Chicago, IL. They manage a network of ~2,000 active carriers and move approximately 500 loads per week across dry van, refrigerated, and flatbed segments. Their operations team currently handles inbound carrier calls manually — dispatchers answer calls from carriers looking for available loads, verify their credentials, negotiate rates, and hand off confirmed bookings to a senior rep.

They are evaluating HappyRobot as a platform to automate the first layer of this process. You are joining the FDE team for a scoping and proof-of-concept engagement based on notes from a Deployment Strategy session with their Carrier Sales team.

> **Important:** the Vice President of Crystal Lane Freight is hesitant to implement this solution because they signed a 3 year contract with a huge call center 3 months ago. He is not convinced that HappyRobot will work and does not understand why they need a Forward Deployed Engineer for this project.

## Problem

Crystal Lane Freight is experiencing significant operational strain on their inbound carrier desk. As load volume has grown, the number of inbound carrier calls has scaled with it — but headcount has not. The result is:

- **Missed calls and long hold times** — during peak hours (Monday morning, Friday afternoon), causing carriers to move on to competing brokers
- **Inconsistent rate negotiation** — different dispatchers apply different ceilings, leading to margin leakage
- **Manual FMCSA verification** — dispatchers look up carrier credentials by hand, introducing both delay and occasional error
- **No audit trail** — there is no structured record of what was said, offered, or agreed to on each call, making disputes difficult to resolve
- **Dispatcher burnout** — high call volume on repetitive, low-complexity interactions leaves less time for relationship management with top carriers

The customer wants to automate the inbound carrier qualification and load matching workflow so that a carrier can call in at any time, get verified, receive a load offer, negotiate a rate, and be handed off to a senior rep — without a dispatcher being involved in the first leg of the conversation.

## Process

The current inbound carrier workflow looks like this:

1. **Carrier calls in** — The carrier dials the brokerage's main operations line and is connected to the next available dispatcher.
2. **Identity and credential check** — The dispatcher asks for the carrier's MC number and manually verifies it against the FMCSA database. They check for active operating authority. If the carrier does not pass, the call ends.
3. **Identity confirmation** — For carriers not already in the system, the carrier receives a confirmation code sent to their registered phone number to verify identity before proceeding.
4. **Load search** — The dispatcher searches the TMS for available loads matching the carrier's lane preference and equipment type. If a match is found, they pitch the load details.

   The following fields are available per load:

   | Field | Description |
   |---|---|
   | `load_id` | Unique identifier for the load |
   | `origin` | Starting location |
   | `destination` | Delivery location |
   | `pickup_datetime` | Date and time for pickup |
   | `delivery_datetime` | Date and time for delivery |
   | `equipment_type` | Type of equipment needed |
   | `loadboard_rate` | Listed rate for the load in load boards |
   | `max_rate` | Maximum the broker will pay the carrier — **never disclosed to the carrier** |
   | `weight` | Load weight in lbs |
   | `commodity_type` | Type of goods |
   | `num_of_pieces` | Number of items |
   | `miles` | Distance to travel |
   | `dimensions` | Size measurements (L x W x H in inches) |
   | `notes` | Additional load notes |

5. **Rate negotiation** — The carrier may accept, reject, or counter. The dispatcher has a rate ceiling they work to — the broker pays the carrier, so the carrier pushes for more and the dispatcher holds the line at `max_rate`. If the carrier won't come down to or below the ceiling, the call ends. If they agree, the booking is tentatively reserved.
6. **Handoff** — Once a rate is agreed, the dispatcher transfers the carrier to a senior rep who confirms the booking, collects additional documentation if needed, and closes the load. Since transfers do not work with web calls, this should be mocked.
7. **Logging** — The dispatcher manually logs the call outcome in the TMS: carrier MC, load ID, agreed rate, outcome, and any notes.

## Dependencies

| System | Role | Access |
|---|---|---|
| Legacy TMS | Source of truth for available loads — origin, destination, equipment, rate, pickup window | TCP socket interface. Fixed-width line protocol. No REST API available. Credentials provided separately. |
| FMCSA API | Carrier authority verification | Public REST API. Requires MC number lookup. |
| HappyRobot Platform | Voice agent, workflow orchestration, data layer, operational UI, SMS sending | Full platform access provided. |
| Senior Rep Queue | Call handoff target once booking is agreed | Mocked — no live transfer required. |

### Legacy TMS — Additional Context

The TMS is a legacy system that predates modern API standards. It exposes load data over a raw TCP socket using a fixed-width, line-based protocol common in older AS/400-style freight platforms. There is no REST API, no JSON, and no documentation beyond what is provided in the protocol spec (link above).

The system is hosted centrally. You will receive a hostname, port, and a personal auth token to connect. It exposes three operations: **load search**, **load detail retrieval**, and **load booking**. The system is known to be unreliable under load — timeouts and malformed responses occur intermittently. Any integration layer built on top of it must handle these gracefully.

## Additional Requirements

### Rate ceiling enforcement

The broker pays the carrier — `max_rate` is the ceiling the brokerage will not exceed. The solution must enforce this during negotiation and must never disclose `max_rate` to the carrier directly or indirectly.

### Carrier identity verification (OTP)

FMCSA verification alone is not sufficient. Once a carrier passes the authority check, an OTP must be sent to their registered phone number and confirmed during the call before load matching proceeds. This is a hard requirement from the brokerage's compliance team.

### Data layer

All call activity must be captured using HappyRobot-native tooling (Twin). External databases may be used only where Twin cannot support the requirement and must be justified.

### Operational UI

A custom internal app (HappyRobot Apps) must surface key operational signals and actions for the brokerage's operations manager — without accessing raw platform logs.

### Evaluation and QA

The delivered solution must include defined northstar KPIs, a scripted test suite covering standard and edge-case scenarios, and adversarial test cases. Results must be documented and presented.

### Deployment

The solution must be containerized (Docker) and deployable with a single command to a cloud environment of the customer's choice.

## Other Considerations

- **Carrier experience** — Carriers are often on the road. The agent must be direct, efficient, and conversational.
- **Negotiation boundaries** — Up to three counter rounds per call. If no deal after three rounds, close professionally and log as failed negotiation — do not transfer.
- **Security** — All endpoints require authentication. The OTP flow must resist social engineering — no bypass under any framing from the carrier.
- **No phone number** — Use the web call trigger feature. No dedicated phone number is to be provisioned.

## Deliverables

Your submission will be sent to Crystal Lane Freight Vice President. It must include all of the following:

1. Summary email to the prospect outlining what was built and next steps
2. Build description document suitable for the brokerage's IT and business review
3. Code repository link
4. HappyRobot platform workflow link
5. Walkthrough video (max 5 min): live demo, dashboard, and QA results
