# Genesys Cloud — integration notes

> These are integration notes compiled for this project, not a Genesys document.
> They cover the fields this pipeline actually extracts. The authoritative reference
> is the Genesys Cloud Developer Center, and anything below should be verified
> against it before being relied on:
> https://developer.genesys.cloud/analyticsdatamanagement/analytics/metrics
>
> They are stored as a file rather than fetched from a URL on purpose: this pins the
> version the descriptions were generated against, so a description can be traced to
> the text that produced it.

## Metric naming convention

Analytics field names encode their own type in the first character, and this is the
most important thing to know before reading any of them.

- `n` prefix: a counter. `nOffered` is a number of interactions, not a null count.
- `t` prefix: a timer. **Every t-prefixed duration is in milliseconds.**
- `o` prefix: an observation or ratio rather than a count or a duration.

The millisecond unit is the single most expensive assumption to get wrong here.
Treating `tTalk` as seconds and dividing by 60 produces an average handle time that
looks entirely plausible and is off by a factor of a thousand. Nothing in the data
signals the unit; the values are bare integers.

## Conversation and participant identifiers

A conversation is one interaction with the contact centre and is identified by
`conversationId`. A conversation contains one or more participants, identified by
`participantId` -- the customer is a participant, each agent is a participant, and
so are automated legs such as IVR. Each participant has one or more sessions,
identified by `sessionId`, one per communication channel used.

The practical consequence: a table at session grain has more rows than there are
conversations, and counting rows to count interactions overstates volume. Aggregate
on `conversationId` when the question is about interactions.

## Conversation attributes

`mediaType` is the channel: `voice`, `chat`, `email` or `callback`.

`direction` is `inbound` or `outbound` from the contact centre's perspective.

`purpose` describes the role a participant played in the conversation: `customer`
is the external party, `agent` is a human handling it, `acd` is the routing engine,
and `ivr` is the automated menu. Filtering to `purpose = 'agent'` is how you get
handled interactions rather than every leg of the routing.

`queueId` is the identifier of the queue the interaction was routed through.
`queueName` is its display label and is editable by administrators, so it changes
over time while the id does not. Join on the id, display the name.

`wrapUpCode` is the disposition the agent selected when closing the interaction.
The available codes are configured per organisation, so the value set is specific to
the deployment and carries no meaning outside it.

## Duration metrics

All values are in milliseconds.

`tAnswered` is how long the interaction waited before being connected to an agent.
It measures the customer's wait, not the agent's work.

`tTalk` is time spent in conversation with the customer.

`tHeld` is time the customer spent on hold during the interaction.

`tAcw` is after-call work: time the agent spent completing the interaction after
the customer disconnected. It is agent-occupied time that does not appear in
`tTalk`, and excluding it understates agent workload.

`tHandle` is the total time an agent was occupied by the interaction. It is the
composite measure and overlaps with the components above, so summing `tTalk`,
`tHeld`, `tAcw` and `tHandle` together double-counts.

`tAbandon` is how long an interaction waited before the customer disconnected
without being connected to an agent.

## Queue aggregate metrics

`nOffered` is the number of interactions routed to the queue.
`nAnswered` is the number connected to an agent.
`nAbandoned` is the number where the customer disconnected while waiting.
`nTransferred` is the number moved to another queue or agent after being answered,
so a transferred interaction was already counted in `nAnswered`.

`oServiceLevel` is the proportion of interactions answered within the configured
service level threshold, expressed as a ratio between 0 and 1, not a percentage.
Charting it without multiplying by 100 produces a service level graph that appears
to sit near zero.

## Personal data

Conversation records relate to identifiable individuals. `conversationId`,
`participantId` and `sessionId` are pseudonymous identifiers for a person's
interaction and are personal data under GDPR even though they contain no name.
Voice and chat transcript content, where extracted, is personal data outright.
