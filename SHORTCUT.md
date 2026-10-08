# TimGym Upload iPhone Shortcut

This shortcut takes workout text from TimGym, asks ChatGPT to structure it, lets you review it, then sends it to your private TimGym account. It uses Apple's **Use Model → ChatGPT** action, not the OpenAI API. API billing is not used. Apple Intelligence and ChatGPT availability and usage limits apply.

## Requirements

- iPhone with Apple Intelligence and the Shortcuts app.
- A TimGym account and its personal API key. Create or rotate it in **ChatGPT connection** on the TimGym dashboard.
- Use Safari at `https://gym.tim0tei.fun`; the app's **Continue in ChatGPT Shortcut** button launches the shortcut by name and passes the text.

Apple documents the ChatGPT model action in Shortcuts [here](https://support.apple.com/guide/shortcuts/use-apple-intelligence-in-shortcuts-tpg3vrvwmclv/ios) and the `shortcuts://run-shortcut` handoff URL [here](https://support.apple.com/en-au/guide/shortcuts/apd624386f42/ios). If the ChatGPT choice is unavailable under **Use Model**, this device or region may not support the Apple Intelligence extension model.

## Build the shortcut

Create a shortcut named exactly **TimGym Upload**. The name must match the app's launch link.

Add these actions in order:

1. **Use Model**
   - Choose **ChatGPT**.
   - Turn on **Follow Up** so ChatGPT can ask for missing reps, sets, or weights before creating the workout.
   - In the prompt below, replace `[Shortcut Input]` with the blue **Shortcut Input** magic variable from the Shortcuts variable picker.
   - Ask it to parse the provided input into one JSON object only, with no Markdown fences:
     ```text
     Turn this workout note into TimGym workout JSON. Return only valid JSON, no code fences or commentary. Do not invent sets, reps, weights, or RPE. If a value is missing, use null where the schema allows it, and ask me to clarify before upload if required information is missing. Use today's date if no date is given.
     Required shape:
     {"date":"YYYY-MM-DD","exercises":[{"name":"Exercise","notes":"","sets":[{"reps":5,"weight_kg":100,"rpe":7}]}]}
     Workout note:
     [Shortcut Input]
     ```
2. **Get Dictionary from Input**
   - Use the response from **Use Model**. This turns the JSON text into a dictionary for the request.
3. **Quick Look**
   - Show the parsed workout. Review it before sending.
4. **Choose from Menu**
   - Add menu options **Save to TimGym** and **Cancel**.
   - Put the request action below **Save to TimGym**, so Cancel does not send anything.
5. Under **Save to TimGym**, add **Get Contents of URL**:
   - URL: `https://gym.tim0tei.fun/api/workouts`
   - Method: **POST**
   - Headers:
     - `Authorization` → `Bearer YOUR_PERSONAL_TIMGYM_KEY`
     - `Content-Type` → `application/json`
   - Request Body: **JSON**; pass the dictionary returned by **Get Dictionary from Input**.
6. Add **Show Result** after the request to display whether TimGym saved it.

Replace `YOUR_PERSONAL_TIMGYM_KEY` with the key shown by TimGym. Keep this shortcut private; anyone who gets a copy containing your key can write workouts to your account. If you rotate the key in TimGym, update the shortcut's header too.

## Use it

1. Open TimGym on iPhone Safari and sign in.
2. Type a workout in **Log with ChatGPT**.
3. Tap **Continue in ChatGPT Shortcut**.
4. Check the structured workout in the Shortcut and choose **Save to TimGym**.

If the shortcut isn't installed yet, the first tap may only show that the shortcut can't be found. Create it with the exact name above, then return to TimGym and tap again.
