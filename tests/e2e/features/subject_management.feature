Feature: Subject Management via ZIP Config Upload
  As a logged-in teacher
  I want to create subjects by uploading a ZIP config
  So that I can quickly set up subjects with assignments

  Background:
    Given the teacher account exists in the database
    And I am logged in as the teacher

  @smoke @subject
  Scenario: Teacher uploads valid ZIP config to create a subject
    Given I am on the teacher dashboard
    When I upload the sample subject config ZIP
    Then the subject "E2E Test Subject" should appear on the dashboard

  @subject @sad-path
  Scenario: Teacher uploads invalid ZIP shows an error
    Given I am on the teacher dashboard
    When I upload an invalid ZIP file
    Then an error message should be visible on the dashboard
