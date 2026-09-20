# Annie demo script — Mac architecture

## Narration and shots

Jeanine often forgets to plug in her phone. When his messages stop going
through, her grandson Ellis worries. Checking on her can mean a two-hour
drive. What if he could ask Annie to help?

[Show the native iPhone app. Ellis opens Ask Annie and sends:]

> Hey Annie, could you ask Grandma to plug in her phone?

The message goes from our Swift iPhone app to the services running on a Mac
in Jeanine's home. The Mac coordinates the task: find Jeanine, deliver the
reminder, listen for her response, and send the result back to Ellis.

[Show Looking for Jeanine, then Annie approaching the designated resident.]

The Mac communicates with Annie over Wi-Fi. Camera perception helps Annie
locate the resident, while navigation uses obstacle checks to constrain
movement.

[Show the message being spoken and the app's delivery progress.]

For this prototype, the Mac's speaker and microphone handle the conversation.
Annie delivers Ellis's reminder, and Jeanine replies:

> Okay, thank you.

[Show the reply arriving in the native app.]

Ellis can see that the reminder was delivered and read Jeanine's response.
That gives him a way to reach her even when her phone is out of charge.

## Recording notes — not narration

- The verified rehearsal uses the real Swift app, family API, errand service
  and dog runtime, with a simulated Go2 and mocked audio. Keep the Simulator
  label visible for that footage; say "In this simulation" before describing
  the robot's actions. Physical movement and live audio still need a recorded
  acceptance run. Do not present mocked speech as a live conversation.
- The shorter text avoids promising a combined wellbeing assessment and
  reminder. The user's longer text ("Hey Annie, my message hasn’t delivered to
  Grandma in the past two days, could you make sure she’s okay and ask her to
  plug in her phone?") routes as a family request after the iOS router fix,
  but the current errand parser selects its explicit phone reminder; it does
  not independently carry out both intentions.
- A reply is not proof that Jeanine is safe or that her phone was plugged in.
  Preserve the actual reply; do not replace "okay, thank you" with a promise
  to charge the phone.
- Physical control uses WebRTC over Wi-Fi, not Bluetooth motion commands.
  Do not name a 7 GB model, Gemma 4, GX10, full DimOS navigation, or a cloud
  summary service as an active part of this verified rehearsal. Its inference
  provider is disabled and family errands use deterministic planning.
- The family errand listens once and returns the reply. The exchange
  "I forgot where I put it" → historical couch observation → second reply
  is not connected to that flow. Keep it out of the current recorded story.
  A separate simulator demonstrated memory retrieval; that does not establish
  this iOS-to-robot integration. A future memory response must cite a real
  observation and distinguish last-seen location from current location.
- Deepgram is an optional cloud speech provider, not on-device speech
  recognition. The isolated rehearsal has it disabled. The narration should
  name it only after testing that selected live audio path.

Evidence and current limits: [control review](CONTROL_REVIEW.md),
[phone handoff](PHONE_DELIVERY.md), and `robot/dog/missions/errand.py`.
