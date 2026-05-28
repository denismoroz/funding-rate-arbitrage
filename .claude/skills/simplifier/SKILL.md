Use this skill when the user asks to simplify, refactor, reduce complexity, clean architecture debt, or make the project easier to maintain.

Rules:
- Prefer deleting code over adding code.
- Do not create new abstractions.
- Do not add new files unless explicitly requested.
- Identify duplicate concepts and unnecessary layers.
- Prefer direct implementation over framework-like design.
- Every proposed change must reduce complexity.

Output:
1. What can be deleted
2. What can be merged
3. What can be renamed
4. What abstractions are unnecessary
5. A small step-by-step cleanup plan