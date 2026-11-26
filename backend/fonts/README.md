# Bundled fonts

One static or variable TTF per font id in SPEC.md section 9, taken unmodified from the
[google/fonts](https://github.com/google/fonts) repository (`ofl/<family>/`) on
2026-11-26, together with the `OFL.txt` published beside each file. Each license text
is in `LICENSES/` named after its font.

| Font id | File | Upstream file | License |
| --- | --- | --- | --- |
| Inter | `Inter.ttf` | `Inter[opsz,wght].ttf` | `LICENSES/Inter.OFL.txt` |
| Poppins | `Poppins.ttf` | `Poppins-Regular.ttf` | `LICENSES/Poppins.OFL.txt` |
| Montserrat | `Montserrat.ttf` | `Montserrat[wght].ttf` | `LICENSES/Montserrat.OFL.txt` |
| Roboto | `Roboto.ttf` | `Roboto[wdth,wght].ttf` | `LICENSES/Roboto.OFL.txt` |
| Playfair Display | `PlayfairDisplay.ttf` | `PlayfairDisplay[wght].ttf` | `LICENSES/PlayfairDisplay.OFL.txt` |
| Bebas Neue | `BebasNeue.ttf` | `BebasNeue-Regular.ttf` | `LICENSES/BebasNeue.OFL.txt` |
| Fira Code | `FiraCode.ttf` | `FiraCode[wght].ttf` | `LICENSES/FiraCode.OFL.txt` |

Only the file names were changed (square brackets and commas are awkward in shells and
in installers); the fonts themselves and their internal family names are untouched, so
the OFL reserved font names still apply to exactly these files.

`backend/scenes/text_scene.py` registers the selected file with Pango before building
the text, because a font that is only present as a file is not visible to the system.
`backend/tests/test_fonts.py` renders one frame per font to prove that works.

To add or replace a font: download it and its `OFL.txt` from google/fonts, drop them
here and in `LICENSES/`, and add the id to `FontId` in `backend/app/models.py` and to
`BUNDLED_FONTS` in the template. The tests fail if any of those four disagree.
