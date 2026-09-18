import os
import re
import sys
import uuid
import django
import requests

# Setup Django environment
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'settings.local_dev')
django.setup()

from django.core.files.base import ContentFile
from wger.core.models import License
from wger.exercises.models import Exercise, ExerciseImage, Translation

FEDB_JSON_URL = 'https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/dist/exercises.json'
IMAGE_BASE_URL = 'https://raw.githubusercontent.com/yuhonas/free-exercise-db/main/exercises/'

def normalize_text(text):
    if not text:
        return ''
    # Lowercase, remove parentheses, hyphens, and non-alphanumeric characters
    text = re.sub(r'\(.*?\)', '', text)
    text = re.sub(r'[^a-z0-9]', '', text.lower())
    return text

def get_tokens(text):
    if not text:
        return set()
    cleaned = re.sub(r'[^a-z0-9\s]', ' ', text.lower())
    # remove very common stop words
    stop = {'with', 'and', 'the', 'on', 'a', 'an', 'in', 'of', 'exercise'}
    return {w for w in cleaned.split() if w and w not in stop}

def run(dry_run=False):
    print("Fetching free-exercise-db database...")
    res = requests.get(FEDB_JSON_URL, timeout=30)
    if res.status_code != 200:
        print(f"Failed to fetch dataset: {res.status_code}")
        return

    fedb_items = res.json()
    print(f"Loaded {len(fedb_items)} exercises from free-exercise-db.")

    # Build lookup dictionaries
    fedb_exact = {}
    fedb_norm = {}
    for item in fedb_items:
        name = item['name'].strip()
        fedb_exact[name.lower()] = item
        norm = normalize_text(name)
        if norm:
            fedb_norm[norm] = item

    # Get CC0 license
    cc0_license = License.objects.filter(short_name='CC0').first()
    if not cc0_license:
        cc0_license = License.objects.first()

    # Find exercises without any images
    missing_exercises = Exercise.objects.filter(exerciseimage__isnull=True).distinct()
    total_missing = missing_exercises.count()
    print(f"Total exercises currently without images: {total_missing}")

    matched = []

    for exercise in missing_exercises:
        # Get all translation names for this exercise, prioritizing English
        translations = list(Translation.objects.filter(exercise=exercise).order_by(
            models_order := '-language__short_name'
        ))
        # Prioritize English translation
        en_trans = [t for t in translations if t.language.short_name == 'en']
        other_trans = [t for t in translations if t.language.short_name != 'en']
        ordered_names = [t.name.strip() for t in (en_trans + other_trans)]

        matched_item = None
        matched_by = None

        for name in ordered_names:
            lower_name = name.lower()
            norm_name = normalize_text(name)

            # 1. Exact match
            if lower_name in fedb_exact:
                matched_item = fedb_exact[lower_name]
                matched_by = f"exact '{name}' -> '{matched_item['name']}'"
                break

            # 2. Normalized match (ignores hyphens, punctuation, spaces)
            if norm_name in fedb_norm:
                matched_item = fedb_norm[norm_name]
                matched_by = f"norm '{name}' -> '{matched_item['name']}'"
                break

            # 3. Special case aliases / variations
            alt_names = []
            if 'kettlebell swing' in lower_name:
                alt_names.extend(['one-arm kettlebell swings', 'kettlebell swing'])
            if 'woodchop' in lower_name:
                alt_names.append('standing cable wood chop')
            if 'ball crunch' in lower_name or 'swiss ball crunch' in lower_name:
                alt_names.append('exercise ball crunch')
            if 'abdominal stabilization' in lower_name:
                alt_names.append('plank')

            # Expand 2 -> two, 1 -> one
            expanded = re.sub(r'\b2\b', 'two', lower_name)
            expanded = re.sub(r'\b1\b', 'one', expanded)
            if expanded != lower_name:
                alt_names.append(expanded)

            for alt in alt_names:
                norm_alt = normalize_text(alt)
                if norm_alt in fedb_norm:
                    matched_item = fedb_norm[norm_alt]
                    matched_by = f"alias '{name}' -> '{matched_item['name']}'"
                    break
            if matched_item:
                break

            # 4. Token subset matching (if all tokens of query match target, or vice versa, min 2 tokens)
            q_tokens = get_tokens(name)
            if len(q_tokens) >= 2:
                for item in fedb_items:
                    t_tokens = get_tokens(item['name'])
                    if len(t_tokens) >= 2 and (
                        q_tokens == t_tokens
                        or (len(q_tokens) >= 2 and q_tokens.issubset(t_tokens))
                        or (len(t_tokens) >= 2 and t_tokens.issubset(q_tokens))
                    ):
                        matched_item = item
                        matched_by = f"tokens '{name}' -> '{item['name']}'"
                        break
            if matched_item:
                break

        if matched_item and matched_item.get('images'):
            matched.append((exercise, matched_item, matched_by))

    print(f"\nMatched {len(matched)} / {total_missing} missing exercises!")

    if dry_run:
        print("\nSample matches:")
        for ex, item, match_info in matched[:15]:
            print(f"  - {match_info}")
        return

    print(f"\nDownloading and importing images for {len(matched)} exercises...")
    success_count = 0
    fail_count = 0

    session = requests.Session()

    for idx, (exercise, item, match_info) in enumerate(matched, start=1):
        images = item.get('images', [])
        if not images:
            continue

        # Import up to 2 images (start and end position)
        for img_idx, rel_path in enumerate(images[:2]):
            full_img_url = f"{IMAGE_BASE_URL}{rel_path}"
            try:
                img_resp = session.get(full_img_url, timeout=15)
                if img_resp.status_code != 200:
                    continue

                ext = os.path.splitext(rel_path)[1] or '.jpg'
                filename = f"{exercise.id}_{img_idx}{ext}"

                ex_img = ExerciseImage(
                    exercise=exercise,
                    is_main=(img_idx == 0),
                    license=cc0_license,
                    license_author='free-exercise-db (yuhonas)',
                    uuid=uuid.uuid4(),
                )
                ex_img.image.save(filename, ContentFile(img_resp.content), save=True)
                success_count += 1
            except Exception as e:
                fail_count += 1
                print(f"Error saving image for {item['name']}: {e}")

        if idx % 25 == 0 or idx == len(matched):
            print(f"Progress: {idx}/{len(matched)} exercises processed ({success_count} images imported)...")

    print(f"\nFinished! Successfully imported {success_count} images for {len(matched)} exercises.")

if __name__ == '__main__':
    dry_run = '--dry-run' in sys.argv
    run(dry_run=dry_run)
