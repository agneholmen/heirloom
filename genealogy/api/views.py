from django.db import transaction
from django.db.models import Q, OuterRef, Subquery, PositiveSmallIntegerField
from django.shortcuts import get_object_or_404

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.viewsets import ModelViewSet
from rest_framework.permissions import IsAuthenticated
from rest_framework.decorators import action

from genealogy.constants import NAMES_REPLACE, SURNAMES_REPLACE
from genealogy.date_functions import extract_year
from genealogy.models import Person, Tree, Family, Child, Event, FamilyEvent, Image, ImagePerson, Archive, Source
from genealogy import gedcom
from genealogy.views.common import get_default_image, get_profile_photo
from .serializers import PersonSearchSerializer, PersonSerializer, TreeSerializer, ArchiveSerializer, SourceSerializer

from functools import reduce

class PersonViewSet(ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PersonSerializer
    
    def get_queryset(self):
        return Person.objects.filter(
            tree__id=self.kwargs["tree_pk"],
            tree__user=self.request.user,
        )

    @transaction.atomic
    def perform_destroy(self, instance):
        instance.delete()
    
    @action(detail=True, methods=['get'])
    def images(self, request, tree_pk=None, pk=None):
        """Get all images for a person"""
        person = self.get_object()
        image_persons = ImagePerson.objects.filter(person=person).select_related('image')
        
        images_data = []
        for ip in image_persons:
            images_data.append({
                'id': ip.image.id,
                'title': ip.image.title,
                'description': ip.image.description,
                'image_url': ip.image.image.url,
                'created': ip.image.created,
                'is_profile': person.profile_image == ip.image if person.profile_image else False
            })
        
        return Response(images_data)
    
    @action(detail=True, methods=['post'])
    def upload_image(self, request, tree_pk=None, pk=None):
        """Upload a new image for a person"""
        person = self.get_object()
        tree = person.tree
        
        if not request.FILES.get('image'):
            return Response(
                {"error": "No image file provided"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        title = request.data.get('title', f'Image of {person.get_name()}')
        description = request.data.get('description', '')
        
        # Create image
        image = Image.objects.create(
            user=request.user,
            tree=tree,
            title=title,
            description=description,
            image=request.FILES['image'],
            private=False
        )
        
        # Link image to person
        ImagePerson.objects.create(person=person, image=image)
        
        return Response({
            'id': image.id,
            'title': image.title,
            'description': image.description,
            'image_url': image.image.url,
            'created': image.created,
            'is_profile': False
        }, status=status.HTTP_201_CREATED)
    
    @action(detail=True, methods=['delete'], url_path='delete_image/(?P<image_id>[^/.]+)')
    def delete_image(self, request, tree_pk=None, pk=None, image_id=None):
        """Delete an image"""
        person = self.get_object()
        
        try:
            image = Image.objects.get(id=image_id, tree=person.tree, user=request.user)
        except Image.DoesNotExist:
            return Response(
                {"error": "Image not found"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # If this is the profile image, clear it
        if person.profile_image == image:
            person.profile_image = None
            person.save()
        
        image.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    @action(detail=True, methods=['patch'], url_path='set_profile_image/(?P<image_id>[^/.]+)')
    def set_profile_image(self, request, tree_pk=None, pk=None, image_id=None):
        """Set an image as profile picture"""
        person = self.get_object()
        
        try:
            image = Image.objects.get(id=image_id, tree=person.tree)
            # Check if image is linked to this person
            ImagePerson.objects.get(person=person, image=image)
        except (Image.DoesNotExist, ImagePerson.DoesNotExist):
            return Response(
                {"error": "Image not found or not linked to this person"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        person.profile_image = image
        person.save()
        
        return Response({
            'message': 'Profile image set successfully',
            'profile_image': image.image.url
        })
    
    @action(detail=True, methods=['post', 'patch'], url_path='update_event/(?P<event_type>[^/.]+)')
    def update_event(self, request, tree_pk=None, pk=None, event_type=None):
        """Create or update a birth/death event"""
        person = self.get_object()
        
        if event_type not in ['birth', 'death']:
            return Response(
                {"error": "Event type must be 'birth' or 'death'"},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        date = request.data.get('date', '')
        place = request.data.get('place', '')
        
        # Get or create the event
        event, created = Event.objects.get_or_create(
            person=person,
            event_type=event_type,
            defaults={
                'date': date,
                'place': place
            }
        )
        
        # Update if it already existed
        if not created:
            event.date = date
            event.place = place
            event.save()
        
        return Response({
            'message': f'{event_type.capitalize()} event {"created" if created else "updated"} successfully',
            'event': {
                'type': event_type,
                'date': event.date,
                'place': event.place
            }
        })

    def _get_single_parent_children_choices(self, person):
        children = Child.objects.filter(
            Q(family__husband=person, family__wife=None) | Q(family__wife=person, family__husband=None)
        ).select_related('person')
        return [(child.id, child.person.get_name_years()) for child in children]

    def _get_partner_family_choices(self, person):
        families = Family.objects.filter(Q(husband=person) | Q(wife=person))
        choices = []
        for family in families:
            choices.append({
                'id': family.id,
                'label': str(family),
            })

        has_single_parent_family = Family.objects.filter(
            (Q(husband=person) & Q(wife=None)) | (Q(wife=person) & Q(husband=None))
        ).exists()

        if not has_single_parent_family:
            choices.append({
                'id': 0,
                'label': f'{person} and unknown partner',
            })

        return choices

    def _parse_id_list(self, raw_value):
        if raw_value is None:
            return []

        if isinstance(raw_value, str):
            if not raw_value.strip():
                return []
            chunks = [item.strip() for item in raw_value.split(',') if item.strip()]
            try:
                return [int(item) for item in chunks]
            except (TypeError, ValueError):
                return None

        if isinstance(raw_value, list):
            try:
                return [int(item) for item in raw_value]
            except (TypeError, ValueError):
                return None

        return None

    @action(detail=True, methods=['get'])
    def life_event_options(self, request, tree_pk=None, pk=None):
        person = self.get_object()

        person_event_types = [
            {'value': value, 'label': label}
            for value, label in Event.EVENT_TYPES
        ]
        family_event_types = [
            {'value': value, 'label': label}
            for value, label in FamilyEvent.EVENT_TYPES
        ]

        families = Family.objects.filter(
            (Q(husband=person) | Q(wife=person)) & Q(husband__isnull=False) & Q(wife__isnull=False)
        )

        family_choices = [
            {
                'id': family.id,
                'label': str(family),
            }
            for family in families
        ]

        sources = Source.objects.filter(tree=person.tree).select_related('archive').order_by('title')
        person_images = ImagePerson.objects.filter(person=person).select_related('image')

        return Response({
            'person_event_types': person_event_types,
            'family_event_types': family_event_types,
            'families': family_choices,
            'sources': [
                {
                    'id': source.id,
                    'title': source.title,
                    'source_type': source.source_type,
                    'source_type_display': source.get_source_type_display(),
                    'url': source.web_link,
                    'archive_title': source.archive.title if source.archive else '',
                }
                for source in sources
            ],
            'images': [
                {
                    'id': image_person.image.id,
                    'title': image_person.image.title,
                    'url': image_person.image.image.url,
                }
                for image_person in person_images
            ],
        })

    @action(detail=True, methods=['post'])
    def add_life_event(self, request, tree_pk=None, pk=None):
        person = self.get_object()

        event_type = request.data.get('event_type')
        date = (request.data.get('date') or '').strip()
        place = (request.data.get('place') or '').strip()
        description = (request.data.get('description') or '').strip()
        source_ids = self._parse_id_list(request.data.get('source_ids', []))
        image_ids = self._parse_id_list(request.data.get('image_ids', []))

        if source_ids is None:
            return Response({'error': 'Invalid source IDs.'}, status=status.HTTP_400_BAD_REQUEST)
        if image_ids is None:
            return Response({'error': 'Invalid image IDs.'}, status=status.HTTP_400_BAD_REQUEST)

        if not event_type:
            return Response({'error': 'Event type is required.'}, status=status.HTTP_400_BAD_REQUEST)

        if not (date or place or description):
            return Response({'error': 'You must fill out at least one of the fields.'}, status=status.HTTP_400_BAD_REQUEST)

        if date and person.get_birth_year() and extract_year(date) < person.get_birth_year():
            return Response({'error': 'Event date is before birth date!'}, status=status.HTTP_400_BAD_REQUEST)
        if date and person.get_death_year() and extract_year(date) > person.get_death_year():
            return Response({'error': 'Event date is after death date!'}, status=status.HTTP_400_BAD_REQUEST)

        person_event_types = {value for value, _ in Event.EVENT_TYPES}
        family_event_types = {value for value, _ in FamilyEvent.EVENT_TYPES}

        if event_type in person_event_types:
            if event_type == 'birth' and person.has_birth_event():
                return Response({'error': 'This person already has a birth event.'}, status=status.HTTP_400_BAD_REQUEST)
            if event_type == 'death' and person.has_death_event():
                return Response({'error': 'This person already has a death event.'}, status=status.HTTP_400_BAD_REQUEST)

            event = Event.objects.create(
                person=person,
                event_type=event_type,
                date=date,
                place=place,
                description=description,
            )

            if source_ids:
                sources = Source.objects.filter(id__in=source_ids, tree=person.tree)
                if sources.count() != len(set(source_ids)):
                    return Response({'error': 'One or more selected sources are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
                event.sources.set(sources)

            if image_ids:
                images = Image.objects.filter(id__in=image_ids, tree=person.tree)
                if images.count() != len(set(image_ids)):
                    return Response({'error': 'One or more selected images are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
                event.images.set(images)

            return Response({
                'message': 'Event added successfully',
                'id': event.id,
            }, status=status.HTTP_201_CREATED)

        if event_type in family_event_types:
            family_id = request.data.get('family')
            if family_id in (None, '', 'null'):
                return Response({'error': 'Select Family is required.'}, status=status.HTTP_400_BAD_REQUEST)

            try:
                family_id = int(family_id)
                family = Family.objects.get(
                    id=family_id,
                    tree=person.tree,
                )
            except (ValueError, TypeError, Family.DoesNotExist):
                return Response({'error': 'Selected family was not found.'}, status=status.HTTP_400_BAD_REQUEST)

            if not (family.husband == person or family.wife == person):
                return Response({'error': 'Selected family does not belong to this person.'}, status=status.HTTP_400_BAD_REQUEST)

            if FamilyEvent.objects.filter(family=family, event_type=event_type).exists():
                return Response({'error': f'This family already has a {event_type} event!'}, status=status.HTTP_400_BAD_REQUEST)

            event = FamilyEvent.objects.create(
                family=family,
                event_type=event_type,
                date=date,
                place=place,
                description=description,
            )

            if source_ids:
                sources = Source.objects.filter(id__in=source_ids, tree=person.tree)
                if sources.count() != len(set(source_ids)):
                    return Response({'error': 'One or more selected sources are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
                event.sources.set(sources)

            if image_ids:
                images = Image.objects.filter(id__in=image_ids, tree=person.tree)
                if images.count() != len(set(image_ids)):
                    return Response({'error': 'One or more selected images are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
                event.images.set(images)

            return Response({
                'message': 'Family event added successfully',
                'id': event.id,
            }, status=status.HTTP_201_CREATED)

        return Response({'error': 'Invalid event type!'}, status=status.HTTP_400_BAD_REQUEST)

    @action(detail=True, methods=['patch', 'delete'], url_path='events/(?P<event_id>[^/.]+)')
    def timeline_event(self, request, tree_pk=None, pk=None, event_id=None):
        person = self.get_object()

        try:
            event = Event.objects.get(id=event_id, person=person)
        except Event.DoesNotExist:
            return Response({'error': 'Event was not found.'}, status=status.HTTP_404_NOT_FOUND)

        if request.method == 'DELETE':
            event.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        date = (request.data.get('date') or '').strip()
        place = (request.data.get('place') or '').strip()
        description = (request.data.get('description') or '').strip()
        has_source_ids = 'source_ids' in request.data
        has_image_ids = 'image_ids' in request.data

        source_ids = self._parse_id_list(request.data.get('source_ids')) if has_source_ids else None
        image_ids = self._parse_id_list(request.data.get('image_ids')) if has_image_ids else None

        if has_source_ids and source_ids is None:
            return Response({'error': 'Invalid source IDs.'}, status=status.HTTP_400_BAD_REQUEST)
        if has_image_ids and image_ids is None:
            return Response({'error': 'Invalid image IDs.'}, status=status.HTTP_400_BAD_REQUEST)

        if not (date or place or description):
            return Response({'error': 'You must fill out at least one of the fields.'}, status=status.HTTP_400_BAD_REQUEST)

        if date:
            event_year = extract_year(date)
            birth_year = person.get_birth_year()
            death_year = person.get_death_year()

            if event.event_type == 'birth':
                if death_year and event_year and event_year > death_year:
                    return Response({'error': 'Birth date is after death date!'}, status=status.HTTP_400_BAD_REQUEST)
            elif event.event_type == 'death':
                if birth_year and event_year and event_year < birth_year:
                    return Response({'error': 'Death date is before birth date!'}, status=status.HTTP_400_BAD_REQUEST)
            else:
                if birth_year and event_year and event_year < birth_year:
                    return Response({'error': 'Event date is before birth date!'}, status=status.HTTP_400_BAD_REQUEST)
                if death_year and event_year and event_year > death_year:
                    return Response({'error': 'Event date is after death date!'}, status=status.HTTP_400_BAD_REQUEST)

        event.date = date
        event.place = place
        event.description = description
        event.save()

        if has_source_ids:
            sources = Source.objects.filter(id__in=source_ids, tree=person.tree)
            if sources.count() != len(set(source_ids)):
                return Response({'error': 'One or more selected sources are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
            event.sources.set(sources)

        if has_image_ids:
            images = Image.objects.filter(id__in=image_ids, tree=person.tree)
            if images.count() != len(set(image_ids)):
                return Response({'error': 'One or more selected images are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
            event.images.set(images)

        return Response({'message': 'Event updated successfully.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['patch', 'delete'], url_path='family-events/(?P<event_id>[^/.]+)')
    def timeline_family_event(self, request, tree_pk=None, pk=None, event_id=None):
        person = self.get_object()

        try:
            event = FamilyEvent.objects.get(id=event_id)
        except FamilyEvent.DoesNotExist:
            return Response({'error': 'Family event was not found.'}, status=status.HTTP_404_NOT_FOUND)

        if not (event.family.husband_id == person.id or event.family.wife_id == person.id):
            return Response({'error': 'Family event does not belong to this person.'}, status=status.HTTP_403_FORBIDDEN)

        if request.method == 'DELETE':
            event.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        date = (request.data.get('date') or '').strip()
        place = (request.data.get('place') or '').strip()
        description = (request.data.get('description') or '').strip()
        has_source_ids = 'source_ids' in request.data
        has_image_ids = 'image_ids' in request.data

        source_ids = self._parse_id_list(request.data.get('source_ids')) if has_source_ids else None
        image_ids = self._parse_id_list(request.data.get('image_ids')) if has_image_ids else None

        if has_source_ids and source_ids is None:
            return Response({'error': 'Invalid source IDs.'}, status=status.HTTP_400_BAD_REQUEST)
        if has_image_ids and image_ids is None:
            return Response({'error': 'Invalid image IDs.'}, status=status.HTTP_400_BAD_REQUEST)

        if not (date or place or description):
            return Response({'error': 'You must fill out at least one of the fields.'}, status=status.HTTP_400_BAD_REQUEST)

        if date:
            event_year = extract_year(date)
            birth_year = person.get_birth_year()
            death_year = person.get_death_year()
            if birth_year and event_year and event_year < birth_year:
                return Response({'error': 'Event date is before birth date!'}, status=status.HTTP_400_BAD_REQUEST)
            if death_year and event_year and event_year > death_year:
                return Response({'error': 'Event date is after death date!'}, status=status.HTTP_400_BAD_REQUEST)

        event.date = date
        event.place = place
        event.description = description
        event.save()

        if has_source_ids:
            sources = Source.objects.filter(id__in=source_ids, tree=person.tree)
            if sources.count() != len(set(source_ids)):
                return Response({'error': 'One or more selected sources are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
            event.sources.set(sources)

        if has_image_ids:
            images = Image.objects.filter(id__in=image_ids, tree=person.tree)
            if images.count() != len(set(image_ids)):
                return Response({'error': 'One or more selected images are invalid.'}, status=status.HTTP_400_BAD_REQUEST)
            event.images.set(images)

        return Response({'message': 'Family event updated successfully.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['get'])
    def relationship_data(self, request, tree_pk=None, pk=None):
        person = self.get_object()

        father = person.get_father()
        mother = person.get_mother()

        partners = []
        families = Family.objects.filter((Q(husband=person) & ~Q(wife=None)) | (Q(wife=person) & ~Q(husband=None)))
        for family in families:
            partner = family.wife if family.husband == person else family.husband
            partners.append({
                'id': partner.id,
                'full_name': partner.get_name_years(),
            })

        children = []
        child_relations = Child.objects.filter(Q(family__husband=person) | Q(family__wife=person)).select_related('person')
        for relation in child_relations:
            children.append({
                'id': relation.person.id,
                'full_name': relation.person.get_name_years(),
            })

        single_parent_children = self._get_single_parent_children_choices(person)

        return Response({
            'father': {'id': father.id, 'full_name': father.get_name_years()} if father else None,
            'mother': {'id': mother.id, 'full_name': mother.get_name_years()} if mother else None,
            'partners': partners,
            'children': children,
            'can_add_father': father is None,
            'can_add_mother': mother is None,
            'partner_family_choices': self._get_partner_family_choices(person),
            'child_family_choices': self._get_partner_family_choices(person),
            'single_parent_children': [
                {'id': child_id, 'label': label}
                for child_id, label in single_parent_children
            ],
        })

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def remove_relationship(self, request, tree_pk=None, pk=None):
        person = self.get_object()

        relationship_type = request.data.get('relationship_type')
        related_person_id = request.data.get('related_person_id')

        try:
            related_person_id = int(related_person_id)
        except (TypeError, ValueError):
            return Response({'error': 'Invalid related person.'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            if relationship_type == 'father':
                this_child = Child.objects.get(person=person, family__husband=related_person_id)
                if this_child.family.wife:
                    wife_family, _ = Family.objects.get_or_create(
                        wife=this_child.family.wife,
                        husband=None,
                        defaults={'tree': this_child.family.tree}
                    )
                    this_child.family = wife_family
                    this_child.save()
                else:
                    this_child.delete()

            elif relationship_type == 'mother':
                this_child = Child.objects.get(person=person, family__wife=related_person_id)
                if this_child.family.husband:
                    husband_family, _ = Family.objects.get_or_create(
                        husband=this_child.family.husband,
                        wife=None,
                        defaults={'tree': this_child.family.tree}
                    )
                    this_child.family = husband_family
                    this_child.save()
                else:
                    this_child.delete()

            elif relationship_type == 'child':
                this_child = Child.objects.get(
                    Q(person=related_person_id) & (Q(family__husband=person) | Q(family__wife=person))
                )
                if (this_child.family.wife == person and not this_child.family.husband) or (
                    this_child.family.husband == person and not this_child.family.wife
                ):
                    if this_child.family.children.count() == 1:
                        this_child.family.delete()
                    else:
                        this_child.delete()
                else:
                    if this_child.family.husband == person:
                        wife_family, _ = Family.objects.get_or_create(
                            wife=this_child.family.wife,
                            husband=None,
                            defaults={'tree': this_child.family.tree}
                        )
                        this_child.family = wife_family
                        this_child.save()
                    else:
                        husband_family, _ = Family.objects.get_or_create(
                            husband=this_child.family.husband,
                            wife=None,
                            defaults={'tree': this_child.family.tree}
                        )
                        this_child.family = husband_family
                        this_child.save()

            elif relationship_type == 'partner':
                this_family = Family.objects.get(
                    Q(husband=person) | Q(wife=person),
                    Q(husband=related_person_id) | Q(wife=related_person_id)
                )
                if this_family.children.count() == 0:
                    this_family.delete()
                else:
                    return Response(
                        {'error': 'Cannot remove partner since they have children together!'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                return Response({'error': 'Invalid relationship type!'}, status=status.HTTP_400_BAD_REQUEST)

        except Child.DoesNotExist:
            return Response({'error': 'Relationship was not found.'}, status=status.HTTP_400_BAD_REQUEST)
        except Family.DoesNotExist:
            return Response({'error': 'Relationship was not found.'}, status=status.HTTP_400_BAD_REQUEST)

        return Response({'message': 'Relationship removed successfully.'}, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    @transaction.atomic
    def add_related_person(self, request, tree_pk=None, pk=None):
        person = self.get_object()

        relation_type = request.data.get('relation_type')
        mode = request.data.get('mode', 'new')

        if relation_type not in ('father', 'mother', 'partner', 'child'):
            return Response({'error': 'Invalid relation type.'}, status=status.HTTP_400_BAD_REQUEST)
        if mode not in ('new', 'existing'):
            return Response({'error': 'Invalid mode.'}, status=status.HTTP_400_BAD_REQUEST)

        if mode == 'new':
            first_name = (request.data.get('first_name') or '').strip()
            last_name = (request.data.get('last_name') or '').strip()
            sex = request.data.get('sex', 'U')

            if not (first_name or last_name):
                return Response({'error': 'You must provide a first name or last name.'}, status=status.HTTP_400_BAD_REQUEST)
            if sex not in ('M', 'F', 'U'):
                return Response({'error': 'Invalid sex value.'}, status=status.HTTP_400_BAD_REQUEST)

            related_person = Person.objects.create(
                tree=person.tree,
                first_name=first_name,
                last_name=last_name,
                sex=sex,
            )

            birth_date = (request.data.get('birth_date') or '').strip()
            birth_place = (request.data.get('birth_place') or '').strip()
            death_date = (request.data.get('death_date') or '').strip()
            death_place = (request.data.get('death_place') or '').strip()

            if birth_date or birth_place:
                Event.objects.create(person=related_person, event_type='birth', date=birth_date, place=birth_place)
            if death_date or death_place:
                Event.objects.create(person=related_person, event_type='death', date=death_date, place=death_place)
        else:
            selected_person_id = request.data.get('selected_person_id')
            try:
                selected_person_id = int(selected_person_id)
                related_person = Person.objects.get(id=selected_person_id, tree=person.tree)
            except (TypeError, ValueError, Person.DoesNotExist):
                return Response({'error': 'Selected person was not found.'}, status=status.HTTP_400_BAD_REQUEST)

        if relation_type in ('father', 'mother'):
            if related_person.id == person.id:
                return Response({'error': 'A person cannot be their own parent.'}, status=status.HTTP_400_BAD_REQUEST)

            if relation_type == 'father' and person.get_father():
                return Response({'error': 'This person already has a father!'}, status=status.HTTP_400_BAD_REQUEST)
            if relation_type == 'mother' and person.get_mother():
                return Response({'error': 'This person already has a mother!'}, status=status.HTTP_400_BAD_REQUEST)
            if mode == 'existing':
                child_link = Child.objects.filter(person=person).first()
                if child_link and (child_link.family.husband == related_person or child_link.family.wife == related_person):
                    return Response({'error': 'This person is already a parent of the selected child!'}, status=status.HTTP_400_BAD_REQUEST)

            child_link = Child.objects.filter(person=person).first()
            if child_link:
                if relation_type == 'father':
                    if child_link.family.wife:
                        existing_family = Family.objects.filter(husband=related_person, wife=child_link.family.wife).first()
                        if existing_family:
                            child_link.family = existing_family
                            child_link.save()
                        else:
                            child_link.family.husband = related_person
                            child_link.family.save()
                    else:
                        child_link.family.husband = related_person
                        child_link.family.save()
                else:
                    if child_link.family.husband:
                        existing_family = Family.objects.filter(husband=child_link.family.husband, wife=related_person).first()
                        if existing_family:
                            child_link.family = existing_family
                            child_link.save()
                        else:
                            child_link.family.wife = related_person
                            child_link.family.save()
                    else:
                        child_link.family.wife = related_person
                        child_link.family.save()
            else:
                family = Family.objects.create(
                    tree=person.tree,
                    husband=related_person if relation_type == 'father' else None,
                    wife=related_person if relation_type == 'mother' else None,
                )
                Child.objects.create(family=family, person=person)

            return Response({'message': 'Parent added successfully.'}, status=status.HTTP_201_CREATED)

        if relation_type == 'child':
            family_value = request.data.get('family', 0)
            try:
                family_value = int(family_value)
            except (TypeError, ValueError):
                return Response({'error': 'Select Family is required.'}, status=status.HTTP_400_BAD_REQUEST)

            if mode == 'existing' and Child.objects.filter(person=related_person).exists():
                return Response({'error': 'The selected person already has parents!'}, status=status.HTTP_400_BAD_REQUEST)

            if family_value == 0:
                family = Family.objects.create(
                    tree=person.tree,
                    husband=person if person.sex == 'M' else None,
                    wife=person if person.sex != 'M' else None,
                )
            else:
                try:
                    family = Family.objects.get(id=family_value, tree=person.tree)
                except Family.DoesNotExist:
                    return Response({'error': 'Selected family was not found.'}, status=status.HTTP_400_BAD_REQUEST)

            Child.objects.create(person=related_person, family=family)
            return Response({'message': 'Child added successfully.'}, status=status.HTTP_201_CREATED)

        if Family.objects.filter(
            (Q(wife=person) & Q(husband=related_person)) | (Q(wife=related_person) & Q(husband=person))
        ).exists():
            return Response({'error': 'The selected people already have a family!'}, status=status.HTTP_400_BAD_REQUEST)

        if (person.get_birth_year() and related_person.get_death_year() and person.get_birth_year() > related_person.get_death_year()) \
            or (person.get_death_year() and related_person.get_birth_year() and related_person.get_birth_year() > person.get_death_year()):
            return Response({'error': 'The selected people were not alive at the same time!'}, status=status.HTTP_400_BAD_REQUEST)

        family_value = request.data.get('family', 0)
        try:
            family_value = int(family_value)
        except (TypeError, ValueError):
            family_value = 0

        selected_children = request.data.get('existing_children', [])
        if not isinstance(selected_children, list):
            selected_children = []
        try:
            selected_children = [int(child_id) for child_id in selected_children]
        except (TypeError, ValueError):
            selected_children = []

        if family_value != 0:
            try:
                family = Family.objects.get(id=family_value, tree=person.tree)
            except Family.DoesNotExist:
                return Response({'error': 'Selected family was not found.'}, status=status.HTTP_400_BAD_REQUEST)

            if person.sex == 'M':
                family.husband = person
                family.wife = related_person
            else:
                family.husband = related_person
                family.wife = person
            family.save()
            return Response({'message': 'Partner added successfully.'}, status=status.HTTP_201_CREATED)

        families = Family.objects.filter((Q(husband=person) & Q(wife=None)) | (Q(wife=person) & Q(husband=None)))
        if families:
            family = Family.objects.get(id=families[0].id)
            if person.sex == 'M':
                family.husband = person
                family.wife = related_person
            else:
                family.husband = related_person
                family.wife = person
            family.save()
        else:
            family = Family.objects.create(
                tree=person.tree,
                husband=person if person.sex == 'M' else related_person,
                wife=related_person if person.sex == 'M' else person,
            )

        all_single_parent_child_ids = [child_id for child_id, _ in self._get_single_parent_children_choices(person)]
        if all_single_parent_child_ids and selected_children and len(selected_children) != len(all_single_parent_child_ids):
            still_single_parent_children = [child_id for child_id in all_single_parent_child_ids if child_id not in selected_children]
            remaining_family = Family.objects.create(
                tree=person.tree,
                husband=person if person.sex == 'M' else None,
                wife=person if person.sex != 'M' else None,
            )
            for child_id in still_single_parent_children:
                child_object = Child.objects.get(id=child_id)
                child_object.family = remaining_family
                child_object.save()

        return Response({'message': 'Partner added successfully.'}, status=status.HTTP_201_CREATED)


class PersonSearchView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        tree = request.query_params.get('tree')

        first_name = request.query_params.get('first_name')
        last_name = request.query_params.get('last_name')
        birth_place = request.query_params.get('birth_place')
        death_place = request.query_params.get('death_place')
        birth_year = request.query_params.get('birth_year')
        death_year = request.query_params.get('death_year')
        birth_year_from = request.query_params.get('birth_year_from')
        birth_year_to = request.query_params.get('birth_year_to')
        death_year_from = request.query_params.get('death_year_from')
        death_year_to = request.query_params.get('death_year_to')

        name_conditions = []
        birth_conditions = []
        death_conditions = []

        if first_name:
            name_strings = first_name.split()
            name_or_conditions = []
            for name in name_strings:
                found_first = False
                for n in NAMES_REPLACE:
                    if name in n:
                        found_first = True
                        for variation in n:
                            name_or_conditions.append(Q(first_name__icontains=variation))
                if not found_first:    
                    name_or_conditions.append(Q(first_name__icontains=name))
            name_or_conditions = reduce(lambda x, y: x | y, name_or_conditions)
            if name_conditions:
                name_conditions = name_conditions & name_or_conditions
            else:
                name_conditions = name_or_conditions

        if last_name:
            name_strings = last_name.split()
            name_or_conditions = []
            for name in name_strings:
                found_last = False
                for n in SURNAMES_REPLACE:
                    if name in n:
                        found_last = True
                        for variation in n:
                            name_or_conditions.append(Q(last_name__icontains=variation))
                if not found_last:
                    name_or_conditions.append(Q(last_name__icontains=name))
            name_or_conditions = reduce(lambda x, y: x | y, name_or_conditions)
            if name_conditions:
                name_conditions = name_conditions & name_or_conditions
            else:
                name_conditions = name_or_conditions
        
        if birth_place:
            birth_conditions.append(Q(event__place__icontains=birth_place))
        if birth_year:
            birth_conditions.append(Q(event__date__icontains=birth_year))
        if birth_year_from:
            birth_conditions.append(Q(event__year__gte=birth_year_from))
        if birth_year_to:
            birth_conditions.append(Q(event__year__lte=birth_year_to))
        if death_place:
            death_conditions.append(Q(event__place__icontains=death_place))
        if death_year:
            death_conditions.append(Q(event__date__icontains=death_year))
        if death_year_from:
            death_conditions.append(Q(event__year__gte=death_year_from))
        if death_year_to:
            death_conditions.append(Q(event__year__lte=death_year_to))

        final_query = Q(tree=tree)
        if name_conditions:
            final_query = final_query & name_conditions

        queryset = Person.objects.filter(final_query)

        if birth_conditions:
            birth_query = Q(event__event_type='birth') & reduce(lambda x, y: x & y, birth_conditions)
            queryset = queryset.filter(birth_query)
        if death_conditions:
            death_query = Q(event__event_type='death') & reduce(lambda x, y: x & y, death_conditions)
            queryset = queryset.filter(death_query)

        serializer = PersonSearchSerializer(queryset, many=True)
        return Response(serializer.data)
    
class TreeViewSet(ModelViewSet):
    serializer_class = TreeSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return Tree.objects.filter(user=self.request.user).order_by('name')

    def create(self, request, *args, **kwargs):
        """
        Custom create to handle GEDCOM file upload and parsing
        """
        # Make data mutable
        data = request.data.copy()
        data['user'] = request.user.id

        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)

        # Save the tree (without GEDCOM first)
        tree = serializer.save()

        # Handle GEDCOM file if provided
        gedcom_file = request.FILES.get('gedcom_file')
        if gedcom_file:
            tree.gedcom_file = gedcom_file
            tree.save(update_fields=['gedcom_file'])  # Save file

            try:
                gedcom.handle_uploaded_file(tree)
            except Exception as e:
                # Optional: delete tree on parse failure?
                tree.delete()
                return Response(
                    {"error": "GEDCOM processing failed", "details": str(e)},
                    status=status.HTTP_400_BAD_REQUEST
                )

        headers = self.get_success_headers(serializer.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

    def perform_destroy(self, instance):
        """
        Optional: clean up GEDCOM file on delete
        """
        if instance.gedcom_file:
            instance.gedcom_file.delete(save=False)
        instance.delete()

    @action(detail=True, methods=['get', 'post'], url_path='archives')
    def archives(self, request, pk=None):
        tree = self.get_object()

        if request.method == 'GET':
            archives = Archive.objects.filter(tree=tree).order_by('title')
            serializer = ArchiveSerializer(archives, many=True)
            return Response(serializer.data)

        serializer = ArchiveSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save(tree=tree)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'delete'], url_path='archives/(?P<archive_id>[^/.]+)')
    def archive_detail(self, request, pk=None, archive_id=None):
        tree = self.get_object()
        archive = get_object_or_404(Archive, id=archive_id, tree=tree)

        if request.method == 'DELETE':
            archive.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = ArchiveSerializer(archive, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data)

    @action(detail=True, methods=['get', 'post'], url_path='sources')
    def sources(self, request, pk=None):
        tree = self.get_object()

        if request.method == 'GET':
            sources = Source.objects.filter(tree=tree).select_related('archive').order_by('title')
            serializer = SourceSerializer(sources, many=True)
            return Response(serializer.data)

        serializer = SourceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        archive = None
        archive_id = serializer.validated_data.get('archive')
        if archive_id:
            archive = get_object_or_404(Archive, id=archive_id.id, tree=tree)

        serializer.save(tree=tree, archive=archive)
        return Response(serializer.data, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=['patch', 'delete'], url_path='sources/(?P<source_id>[^/.]+)')
    def source_detail(self, request, pk=None, source_id=None):
        tree = self.get_object()
        source = get_object_or_404(Source, id=source_id, tree=tree)

        if request.method == 'DELETE':
            source.delete()
            return Response(status=status.HTTP_204_NO_CONTENT)

        serializer = SourceSerializer(source, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)

        archive = serializer.validated_data.get('archive', source.archive)
        if archive:
            archive = get_object_or_404(Archive, id=archive.id, tree=tree)

        serializer.save(archive=archive)
        return Response(serializer.data)

    @action(detail=True, methods=['get'], url_path='data_quality')
    def data_quality(self, request, pk=None):
        tree = self.get_object()

        items = []
        for person in Person.objects.filter(tree=tree).order_by('last_name', 'first_name', 'id'):
            warnings = person.get_data_quality_warnings()
            if not warnings:
                continue

            items.append({
                'person': {
                    'id': person.id,
                    'name': person.get_name_years() or f'Person #{person.id}',
                },
                'warning_count': len(warnings),
                'warnings': warnings,
            })

        return Response({
            'tree_id': tree.id,
            'tree_name': tree.name,
            'people_with_warnings': len(items),
            'total_warnings': sum(item['warning_count'] for item in items),
            'items': items,
        })
    
    @action(detail=True, methods=['get'], url_path='view/(?P<person_pk>[^/.]+)')
    def tree_view(self, request, pk=None, person_pk=None):
        """
        Get tree data for visualization
        Returns: Tree data with family structure for the specified person
        """
        tree = self.get_object()
        
        # Get person count
        person_count = Person.objects.filter(tree=tree).count()
        
        # If no people in tree
        if person_count == 0:
            return Response({
                'tree_id': tree.id,
                'tree_name': tree.name,
                'person_count': 0,
                'tree_data': None
            })
        
        # If person_pk is 0, get the first person
        if person_pk == '0':
            first_person = Person.objects.filter(tree=tree).order_by('id').first()
            if not first_person:
                return Response({
                    'tree_id': tree.id,
                    'tree_name': tree.name,
                    'person_count': 0,
                    'tree_data': None
                })
            person_pk = first_person.id
        
        # Get the specified person
        try:
            first_person = Person.objects.get(pk=person_pk, tree=tree)
        except Person.DoesNotExist:
            return Response(
                {"error": "Person not found in this tree"},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Build tree data
        generations = 3
        people_data = self._get_person_tree_data(first_person, tree.id)
        people_data['parents'] = self._tree_get_parents(first_person, 1, generations, tree.id)
        
        # Get partner and children
        family = Family.objects.filter(Q(husband=first_person) | Q(wife=first_person)).first()
        if family:
            if family.husband == first_person and family.wife:
                people_data['partner'] = self._get_person_tree_data(family.wife, tree.id)
            elif family.wife == first_person and family.husband:
                people_data['partner'] = self._get_person_tree_data(family.husband, tree.id)
            
            # Get children
            birth_year_subquery = Event.objects.filter(
                person=OuterRef('person'), 
                event_type='birth'
            ).values('year')[:1]
            
            children = Child.objects.filter(family=family).annotate(
                birth_year=Subquery(birth_year_subquery, output_field=PositiveSmallIntegerField())
            ).order_by('birth_year')
            
            if children:
                people_data['children'] = []
                for child in children:
                    people_data['children'].append(self._get_person_tree_data(child.person, tree.id))
        
        return Response({
            'tree_id': tree.id,
            'tree_name': tree.name,
            'person_count': person_count,
            'tree_data': people_data
        })
    
    def _get_person_tree_data(self, person, tree_id):
        """Helper method to get person data for tree visualization"""
        return {
            'first_name': person.first_name,
            'last_name': person.last_name,
            'id': person.id,
            'image': get_default_image(person.sex) if not person.profile_image else get_profile_photo(person),
            'years': person.get_years(),
            'person_url': f'/tree/{tree_id}/person/{person.id}',
            'tree_url': f'/tree/{tree_id}/person/{person.id}',
        }
    
    def _tree_get_parents(self, current_person, generation, max_generation, tree_id):
        """Helper method to recursively get parents"""
        if generation == max_generation:
            return []
        
        parents = []
        father = current_person.get_father()
        mother = current_person.get_mother()
        
        if father:
            parents.append({
                'first_name': father.first_name,
                'last_name': father.last_name,
                'id': father.id,
                'image': get_default_image(father.sex) if not father.profile_image else get_profile_photo(father),
                'years': father.get_years(),
                'person_url': f'/tree/{tree_id}/person/{father.id}',
                'tree_url': f'/tree/{tree_id}/person/{father.id}',
                'parent_type': 'father',
                'parents': self._tree_get_parents(father, generation + 1, max_generation, tree_id)
            })
        else:
            parents.append({
                'id': 0,
                'child_id': current_person.id,
                'person_url': f'/api/genealogy/person/{current_person.id}/add-parent/father',
                'parent_type': 'father',
                'parents': []
            })
        
        if mother:
            parents.append({
                'first_name': mother.first_name,
                'last_name': mother.last_name,
                'id': mother.id,
                'image': get_default_image(mother.sex) if not mother.profile_image else get_profile_photo(mother),
                'years': mother.get_years(),
                'person_url': f'/tree/{tree_id}/person/{mother.id}',
                'tree_url': f'/tree/{tree_id}/person/{mother.id}',
                'parent_type': 'mother',
                'parents': self._tree_get_parents(mother, generation + 1, max_generation, tree_id)
            })
        else:
            parents.append({
                'id': 0,
                'child_id': current_person.id,
                'person_url': f'/api/genealogy/person/{current_person.id}/add-parent/mother',
                'parent_type': 'mother',
                'parents': []
            })
        
        return parents