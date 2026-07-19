from rest_framework import serializers
from genealogy.models import Person, Tree, Archive, Source

class TreeSerializer(serializers.ModelSerializer):
    people = serializers.SerializerMethodField()
    gedcom_file = serializers.FileField(write_only=True, required=False)

    class Meta:
        model = Tree
        fields = ['user', 'id', 'name', 'description', 'upload_date', 'private', 'people', 'gedcom_file']
        read_only_fields = ['id', 'upload_date', 'people']

    def get_people(self, obj):
        return Person.objects.filter(tree=obj).count()

    def validate_name(self, value):
        if not value or not value.strip():
            raise serializers.ValidationError("Tree name is required.")
        if len(value.strip()) > 100:
            raise serializers.ValidationError("Tree name cannot exceed 100 characters.")
        return value.strip()
    
class PersonSearchSerializer(serializers.ModelSerializer):
    birth_year = serializers.SerializerMethodField()
    death_year = serializers.SerializerMethodField()

    class Meta:
        model = Person
        fields = ['id', 'first_name', 'last_name', 'birth_year', 'death_year', 'tree']

    def get_birth_year(self, obj):
        return obj.get_birth_year()
    
    def get_death_year(self, obj):
        return obj.get_death_year()
    
class PersonSerializer(serializers.ModelSerializer):
    sex_display = serializers.CharField(source='get_sex_display', read_only=True)
    details = serializers.SerializerMethodField()
    profile_image = serializers.SerializerMethodField()

    class Meta:
        model = Person
        fields = ['id', 'first_name', 'last_name', 'tree', 'sex', 'sex_display', 'details', 'profile_image']
        read_only_fields = ['id', 'tree', 'sex_display', 'details', 'profile_image']
    
    def get_profile_image(self, obj):
        """Return profile image URL or default avatar"""
        from genealogy.views.common import get_default_image, get_profile_photo
        
        if obj.profile_image:
            return get_profile_photo(obj)
        return get_default_image(obj.sex)
    
    def get_relatives(self, obj):
        return obj.get_family_data()
    
    def get_details(self, obj):
        return obj.get_details_data()


class ArchiveSerializer(serializers.ModelSerializer):
    class Meta:
        model = Archive
        fields = ['id', 'tree', 'title', 'archive_id', 'publisher', 'author', 'description', 'web_link']
        read_only_fields = ['id', 'tree']


class SourceSerializer(serializers.ModelSerializer):
    archive_title = serializers.CharField(source='archive.title', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)

    class Meta:
        model = Source
        fields = [
            'id',
            'tree',
            'archive',
            'archive_title',
            'title',
            'source_type',
            'source_type_display',
            'citation_text',
            'web_link',
            'description',
            'publishing_date',
        ]
        read_only_fields = ['id', 'tree', 'archive_title', 'source_type_display']
