# Exercise catalog

`exercises.json` is derived from [Free Exercise DB](https://github.com/yuhonas/free-exercise-db) by yuhonas
(released under the [Unlicense](https://unlicense.org/), public domain), itself based on
[exercises.json](https://github.com/wrkout/exercises.json) by Ollie Jennings. It keeps the id, name, category,
equipment, level, muscles, instructions, and image paths of each exercise.

Images are not stored here. TimGym downloads an exercise's photo from
`https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/<path>` the first time it is shown
and caches it in the data volume (`exercise-images/`).

To refresh: download `dist/exercises.json` from the repository and run the trimming step described in the
commit that added this file.
