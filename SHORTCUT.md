# TimGym Upload iPhone Shortcut

This is an optional iPhone Shortcut workflow. The main TimGym dashboard now interprets workouts with a local model on your CasaOS server, so you do not need this Shortcut to log workouts. This alternate workflow uses your iPhone’s on-device model and sends the reviewed workout to your TimGym account.

## Requirements

- iPhone with the Shortcuts app and an available on-device model.
- A TimGym account and its personal API key. Create or rotate it on the **Connection** page in TimGym.
- Open the shortcut in Shortcuts and provide workout text as its input. The dashboard no longer launches this optional shortcut.

Apple documents the `shortcuts://run-shortcut` handoff URL [here](https://support.apple.com/en-au/guide/shortcuts/apd624386f42/ios).

## Build the shortcut

Create a shortcut named exactly **TimGym Upload**. The name must match the app's launch link.

Add these actions in order:

1. **Use Model**
   - Choose the **on-device model**.
   - In the prompt below, replace `[Shortcut Input]` with the blue **Shortcut Input** magic variable from the Shortcuts variable picker.
   - Ask it to parse the provided input into one JSON object only, with no Markdown fences:
     ```text
     Turn this workout note into TimGym JSON. Return only valid JSON, with no code fences or commentary. Use exactly this shape:
     {"date":"YYYY-MM-DD","exercises":[{"name":"Exercise","notes":"","sets":[{"reps":5,"weight_kg":100}]}]}
     Always use the top-level key "exercises" as a list, even for one exercise. Keep weight_kg numeric in kilograms. Do not add RPE, estimated 1RM, totals, or other fields. Include only notes from my input; otherwise use an empty string. Use today's date in YYYY-MM-DD format if no date is given. Do not guess missing reps or weights; ask me to clarify.
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

1. Open the **TimGym Upload** shortcut in Shortcuts.
2. Provide workout text as the shortcut input, such as by selecting text and sharing it to the shortcut.
3. Check the structured workout and choose **Save to TimGym**.

To use this optional route, run the shortcut from the iOS share sheet with workout text selected as input. It operates separately from the dashboard’s server-side model.
