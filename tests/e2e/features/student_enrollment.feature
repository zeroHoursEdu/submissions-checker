Feature: Student Enrollment via CSV Upload
  As a logged-in teacher
  I want to enroll students in a subject via CSV upload
  So that students can access assignments and submit work

  Background:
    Given the teacher account exists in the database
    And I am logged in as the teacher
    And the E2E test subject exists

  @smoke @enrollment
  Scenario: Teacher uploads valid student CSV to enroll students
    Given I am on the subject page for the E2E test subject
    When I import the student CSV into the subject
    Then the enrolled student count should increase
    And the student "Test Student" should be visible in the enrolled list

  @enrollment
  Scenario: Student credentials are retrievable after enrollment
    Given the student "e2e.student@test.example" has been enrolled via CSV
    Then I can retrieve their login credentials from the system
    And the credentials are stored in the context for later use
